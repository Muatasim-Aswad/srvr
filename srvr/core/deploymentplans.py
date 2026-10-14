"""Plans for staged source, image and registered configuration deployments."""

from __future__ import annotations

import base64
import json
from argparse import Namespace
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import configsnapshots, configuration, deploymentprep, deploys, envfile
from .compose import Compose
from .composefile import find_image, set_image, validate_image_ref
from .context import Context
from .errors import SrvrError
from .git import Checkout
from .lock import service_lock
from .onboarding import selected_services
from .registry import Service
from .startup import migration_components


@dataclass
class _Guard:
    files: dict[str, configsnapshots.FileState]
    active: bool = True

    def __call__(self) -> None:
        if self.active:
            configsnapshots.validate(self.files)

    def release(self) -> None:
        self.active = False


def _guard(ctx: Context, service: Service) -> _Guard:
    files = {
        str(ctx.paths.service_state(service.name) / name): configsnapshots.read(
            ctx.paths.service_state(service.name) / name
        )
        for name in ("images.compose.json", "last-deploy.json")
    }
    return _Guard(files)


@dataclass
class Plan:
    current: str
    target: str
    prepare: Callable[[], None]  # make the target version ready to start
    revert: Callable[[], None]  # undo `prepare` before anything was switched
    commits: list[str] = field(default_factory=list)
    warning: str | None = None
    component: str | None = None
    validate: Callable[[], None] = lambda: None
    activate: Callable[[], None] = lambda: None
    release_guard: Callable[[], None] = lambda: None
    selected: tuple[str, ...] = ()
    before_images: dict[str, str] = field(default_factory=dict)
    prepared_images: dict[str, str] = field(default_factory=dict)
    migration_jobs: tuple[str, ...] = ()
    before_configuration: dict[str, configsnapshots.FileState] | None = None


def plan_git(ctx: Context, service: Service, args: Namespace) -> Plan | None:
    guard = _guard(ctx, service)
    expected_configuration = configuration.revision(service)
    checkout = ctx.checkout(service)
    if not checkout.is_clean():
        raise SrvrError(f"{service.name}: {service.workdir} has local changes; resolve them first")
    assert service.branch
    selected_ref = args.ref or service.branch
    ctx.ui.step(f"{service.name}: fetching origin/{selected_ref}")
    if args.ref:
        target_ref = checkout.fetch_ref(args.ref)
    else:
        target_ref = (
            f"refs/remotes/origin/{service.branch}" if checkout.fetch(service.branch) else None
        )
    if target_ref is None:
        raise SrvrError(f"{service.name}: cannot fetch origin/{selected_ref}")

    current = checkout.head()
    target = checkout.resolve(target_ref)
    if target == current and not args.force:
        ctx.ui.ok(f"{service.name}: up to date at {checkout.describe(current)}")
        return None
    forward = checkout.is_ancestor(current, target)
    if not forward and not args.ref:
        raise SrvrError(
            f"{service.name}: origin/{service.branch} is not ahead of the deployed commit; "
            "use --ref to deploy it deliberately"
        )

    return _git_actions(
        ctx,
        service,
        checkout,
        current,
        target,
        target_ref=target_ref,
        forward=forward,
        guard=guard,
        expected_configuration=expected_configuration,
    )


def _git_actions(
    ctx: Context,
    service: Service,
    checkout: Checkout,
    current: str,
    target: str,
    *,
    target_ref: str,
    forward: bool,
    guard: _Guard,
    expected_configuration: str,
) -> Plan:
    def validate() -> None:
        guard()
        configuration.validate(service, expected_configuration)
        if checkout.head() != current or not checkout.is_clean():
            raise SrvrError(f"{service.name}: checkout changed; rerun the update")
        if checkout.resolve(target_ref) != target:
            raise SrvrError(f"{service.name}: target ref changed; rerun the update")

    plan = Plan(
        current=checkout.describe(current),
        target=checkout.describe(target),
        prepare=lambda: None,
        revert=lambda: None,
        commits=checkout.log(current, target) if forward else [],
        warning=None if forward else "the target is not ahead of the current commit",
        validate=validate,
    )
    activated = False
    activated_revision: str | None = None
    target_model: dict[str, object] | None = None

    def prepare() -> None:
        nonlocal target_model
        with deploymentprep.Candidate(ctx, service, commit=target) as candidate:
            target_model = candidate.comparable_model()
            plan.prepared_images = candidate.prepare(
                stamp=ctx.checkout(candidate.compose.service).build_stamp()
            )
            plan.migration_jobs = tuple(
                migration_components(service, selected_services(service, candidate.prepared_model))
            )
        validate()

    def activate() -> None:
        nonlocal activated, activated_revision
        validate()
        checkout.move_to(target)
        activated = True
        activated_revision = configuration.revision(service)
        model = ctx.compose(service).config(all_profiles=True)
        deploymentprep.check_model(service, model)
        if model != target_model:
            raise SrvrError(
                "activated Git configuration differs from prepared target (values hidden)"
            )

    def revert() -> None:
        if activated:
            assert activated_revision is not None
            configuration.validate(service, activated_revision)
            if checkout.head() != target or not checkout.is_clean():
                raise SrvrError("checkout changed during preparation; automatic recovery refused")
            checkout.move_to(current)

    plan.prepare, plan.activate, plan.revert = prepare, activate, revert
    plan.release_guard = guard.release
    return plan


def _assignments(values: list[str], option: str) -> dict[str, str]:
    result = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item or key in result:
            raise SrvrError(f"{option}: use distinct NAME=VALUE selections (values hidden)")
        result[key] = item
    return result


def _summary(images: dict[str, str]) -> str:
    return ", ".join(
        f"{name}={envfile.display_value('IMAGE', ref)}" for name, ref in images.items()
    )


def states(ctx: Context, service: Service) -> dict[str, configsnapshots.FileState]:
    return configsnapshots.capture(
        service, extra=(configsnapshots.version_file(ctx.paths, service),)
    )


def _select_images(
    ctx: Context,
    service: Service,
    args: Namespace,
    active: dict[str, Any],
    *,
    named: bool,
) -> dict[str, str] | None:
    components = [name for name, spec in active.items() if "image" in spec]
    selections = _assignments(getattr(args, "set_image", []), "--set-image")
    component = getattr(args, "component", None) or service.update_component
    if selections and (args.image or getattr(args, "component", None)):
        raise SrvrError("use --set-image or --component/--image together, not both forms")
    if not selections and not args.image:
        current = _summary(
            deploymentprep.display_refs(
                ctx, service, {name: active[name]["image"] for name in components}
            )
        )
        if args.check or not named:
            ctx.ui.line(
                f"{service.name}: pinned to {current}; "
                + (
                    "pass --image to plan an upgrade"
                    if args.check
                    else "skipped (upgrade with --image)"
                )
            )
            return None
        raise SrvrError(f"{service.name}: pass --image or --set-image; srvr never picks a version")
    if not selections:
        if component is None:
            if len(components) != 1:
                raise SrvrError(f"{service.name}: choose --component from {', '.join(components)}")
            component = components[0]
        selections = {component: args.image}
    if set(selections) - set(components):
        raise SrvrError(f"{service.name}: selection is not an image component")
    for ref in selections.values():
        try:
            validate_image_ref(ref)
        except SrvrError as exc:
            raise SrvrError("selected image needs a valid explicit version (value hidden)") from exc
    selected_refs = {name: active[name]["image"] for name in selections}
    if selections == selected_refs and not args.force:
        ctx.ui.ok(f"{service.name}: already pinned to {_summary(selected_refs)}")
        return None
    return selections


def plan_image(ctx: Context, service: Service, args: Namespace, *, named: bool) -> Plan | None:
    guard = _guard(ctx, service)
    expected_configuration = configuration.revision(service)
    original_states = states(ctx, service)
    compose = deploymentprep.compose(ctx, service)
    model = compose.config(all_profiles=True)
    deploymentprep.check_model(service, model)
    active = compose.config()["services"]
    if service.migrate:
        active[service.migrate] = model["services"][service.migrate]
    selections = _select_images(ctx, service, args, active, named=named)
    if selections is None:
        return None
    current = {name: active[name]["image"] for name in selections}
    expected = deepcopy(model)
    for name, ref in selections.items():
        expected["services"][name]["image"] = ref
    edits, owned = _image_edits(ctx, service, selections, current)
    return _image_actions(
        ctx,
        service,
        guard=guard,
        expected_configuration=expected_configuration,
        original_states=original_states,
        compose=compose,
        model=model,
        active=active,
        selections=selections,
        current=current,
        expected=expected,
        edits=edits,
        owned=owned,
    )


def _image_edits(
    ctx: Context,
    service: Service,
    selections: dict[str, str],
    current: dict[str, str],
) -> tuple[dict[Path, bytes], dict[str, str]]:
    edits: dict[Path, bytes] = {}
    owned = deploymentprep.versions(ctx, service)
    for name, ref in selections.items():
        literal = False
        if name not in owned:
            for file in reversed(service.compose_files):
                try:
                    _, found = find_image(file.read_text(), name)
                except SrvrError:
                    continue
                if (
                    found == current[name]
                    and "$" not in found
                    and "&" not in found
                    and "*" not in found
                ):
                    text = edits.get(file, file.read_bytes()).decode()
                    edits[file] = set_image(text, name, ref).encode()
                    literal = True
                break
        if not literal:
            owned[name] = ref
    if owned:
        version = configsnapshots.version_file(ctx.paths, service)
        edits[version] = (
            json.dumps({"services": {name: {"image": ref} for name, ref in owned.items()}}) + "\n"
        ).encode()

    return edits, owned


def _image_actions(
    ctx: Context,
    service: Service,
    *,
    guard: _Guard,
    expected_configuration: str,
    original_states: dict[str, configsnapshots.FileState],
    compose: Compose,
    model: dict[str, Any],
    active: dict[str, Any],
    selections: dict[str, str],
    current: dict[str, str],
    expected: dict[str, Any],
    edits: dict[Path, bytes],
    owned: dict[str, str],
) -> Plan:
    def validate() -> None:
        guard()
        configuration.validate(service, expected_configuration)
        configsnapshots.validate(original_states)
        if compose.config(all_profiles=True) != model:
            raise SrvrError("Compose configuration changed; rerun the update (values hidden)")

    display_current = deploymentprep.display_refs(ctx, service, current)
    plan = Plan(
        current=next(iter(display_current.values()))
        if len(current) == 1
        else _summary(display_current),
        target=envfile.display_value("IMAGE", next(iter(selections.values())))
        if len(selections) == 1
        else _summary(selections),
        prepare=lambda: None,
        revert=lambda: None,
        validate=validate,
        warning="read the upstream release notes before continuing",
        component=next(iter(selections)) if len(selections) == 1 else None,
        selected=tuple(selections),
    )
    changed_states: dict[str, configsnapshots.FileState] | None = None

    def prepare() -> None:
        staged_files = {
            path: content
            for path, content in edits.items()
            if path != configsnapshots.version_file(ctx.paths, service)
        }
        with deploymentprep.Candidate(ctx, service, staged_files, images=owned) as candidate:
            if candidate.comparable_model() != expected:
                raise SrvrError("image edit did not change only selected images (values hidden)")
            missing = tuple(name for name in active if name not in plan.before_images)
            images = candidate.prepare(components=tuple(sorted({*selections, *missing})))
            plan.migration_jobs = tuple(
                migration_components(service, selected_services(service, candidate.prepared_model))
            )
        plan.prepared_images = {
            name: image
            for name, image in plan.before_images.items()
            if name in active and name not in selections
        }
        plan.prepared_images.update(images)
        validate()

    def activate() -> None:
        nonlocal changed_states
        validate()
        targets = dict(original_states)
        for path, content in edits.items():
            state = targets[str(path)]
            targets[str(path)] = configsnapshots.content(state, content)
        configsnapshots.restore(ctx.paths, service, targets, original_states)
        changed_states = states(ctx, service)
        if deploymentprep.compose(ctx, service).config(all_profiles=True) != expected:
            raise SrvrError("activated configuration differs from prepared images (values hidden)")

    def revert() -> None:
        nonlocal changed_states
        if (ctx.paths.service_state(service.name) / "configuration-recovery.json").exists():
            configsnapshots.recover(ctx.paths, service)
            changed_states = states(ctx, service)
        if changed_states is not None:
            configsnapshots.restore(ctx.paths, service, original_states, changed_states)

    plan.prepare, plan.activate, plan.revert = prepare, activate, revert
    plan.release_guard = guard.release
    return plan


def plan_configuration(ctx: Context, service: Service, args: Namespace) -> Plan:
    baseline = configsnapshots.load_baseline(ctx.paths, service)
    original = states(ctx, service)
    if set(baseline) != set(original):
        raise SrvrError("baseline paths differ from registration; review required")
    guard = _guard(ctx, service)
    expected_revision = configuration.revision(service)
    baseline_path = configsnapshots.baseline_file(ctx.paths, service)
    baseline_revision = configsnapshots.read(baseline_path)
    targets = dict(original)
    candidates = _assignments(getattr(args, "config_file", []), "--config-file")
    allowed = {str(path) for path in configuration.files(service)}
    sources = {}
    for path, candidate in candidates.items():
        if path not in allowed:
            raise SrvrError("--config-file target must be an exact registered path (path hidden)")
        source = Path(candidate)
        state = configsnapshots.read(source)
        if state.content is None:
            raise SrvrError("configuration candidate is missing (path hidden)")
        sources[str(source)] = state
        targets[path] = configsnapshots.content(original[path], base64.b64decode(state.content))
    changed = [path for path in targets if targets[path].content != baseline[path].content]
    if service.is_git:
        checkout = ctx.checkout(service)
        if not checkout.is_clean():
            raise SrvrError(
                "source checkout has local changes; deploy repository changes with --ref"
            )
        tracked = ctx.shell.run(["git", "-C", str(service.workdir), "ls-files", "-z"]).stdout.split(
            "\0"
        )
        owned = {str(service.workdir / path) for path in tracked if path}
        if owned.intersection(changed):
            raise SrvrError("repository-owned configuration must be deployed with --ref")
    if not changed and not args.force:
        raise SrvrError("configuration matches the known-good baseline; use --force to redeploy")

    return _configuration_actions(
        ctx,
        service,
        guard=guard,
        expected_revision=expected_revision,
        original=original,
        sources=sources,
        baseline_path=baseline_path,
        baseline_revision=baseline_revision,
        targets=targets,
        baseline=baseline,
        changed=len(changed),
    )


def _configuration_actions(
    ctx: Context,
    service: Service,
    *,
    guard: _Guard,
    expected_revision: str,
    original: dict[str, configsnapshots.FileState],
    sources: dict[str, configsnapshots.FileState],
    baseline_path: Path,
    baseline_revision: configsnapshots.FileState,
    targets: dict[str, configsnapshots.FileState],
    baseline: dict[str, configsnapshots.FileState],
    changed: int,
) -> Plan:
    def validate() -> None:
        guard()
        configuration.validate(service, expected_revision)
        configsnapshots.validate(original)
        configsnapshots.validate(sources)
        if configsnapshots.read(baseline_path) != baseline_revision:
            raise SrvrError("configuration baseline changed; rerun the update")

    plan = Plan(
        "known-good configuration",
        f"configuration ({changed} changed files; values hidden)",
        lambda: None,
        lambda: None,
        validate=validate,
        before_configuration=baseline,
    )
    switched: dict[str, configsnapshots.FileState] | None = None
    target_model: dict[str, object] | None = None

    def prepare() -> None:
        nonlocal target_model
        content = {
            Path(path): base64.b64decode(state.content)
            for path, state in targets.items()
            if state.content is not None and state != original[path]
        }
        with deploymentprep.Candidate(
            ctx, service, content, commit=ctx.checkout(service).head() if service.is_git else None
        ) as candidate:
            target_model = candidate.comparable_model()
            stamp = ctx.checkout(service).build_stamp() if service.is_git else None
            plan.prepared_images = candidate.prepare(stamp=stamp)
            plan.migration_jobs = tuple(
                migration_components(service, selected_services(service, candidate.prepared_model))
            )
        validate()

    def activate() -> None:
        nonlocal switched
        validate()
        configsnapshots.restore(ctx.paths, service, targets, original)
        switched = states(ctx, service)
        if deploymentprep.compose(ctx, service).config(all_profiles=True) != target_model:
            raise SrvrError("activated configuration differs from prepared target (values hidden)")

    def revert() -> None:
        nonlocal switched
        if (ctx.paths.service_state(service.name) / "configuration-recovery.json").exists():
            configsnapshots.recover(ctx.paths, service)
            switched = states(ctx, service)
        if switched is not None:
            configsnapshots.restore(ctx.paths, service, original, switched)

    plan.prepare, plan.activate, plan.revert = prepare, activate, revert
    plan.release_guard = guard.release
    return plan


def capture_configuration(ctx: Context, service: Service, args: Namespace) -> None:
    expected = configuration.revision(service)
    captured = states(ctx, service)
    file = configsnapshots.baseline_file(ctx.paths, service)
    if file.exists():
        raise SrvrError(
            "known-good baseline already exists; only a successful deployment replaces it"
        )
    ctx.ui.line("Capture current known-good registered configuration privately (values hidden)")
    ctx.ui.confirm(f"Capture {service.name} configuration?", assume_yes=args.yes)
    with service_lock(ctx.paths, service.name):
        deploys.assert_recovered(ctx.paths, service.name)
        configuration.validate(service, expected)
        configsnapshots.validate(captured)
        if file.exists():
            raise SrvrError("configuration baseline changed; rerun the command")
        deploymentprep.check_model(
            service, deploymentprep.compose(ctx, service).config(all_profiles=True)
        )
        if not deploys.wait_ready(ctx, service).ok:
            raise SrvrError("current installation must be healthy before capturing its baseline")
        configsnapshots.save_baseline(ctx.paths, service, captured)
    ctx.ui.ok("known-good configuration baseline captured privately")
