function Link(link)
  local path, anchor = link.target:match('^([^#]*)(.*)$')
  if not path:match('^[%a][%w+.-]*:') and path:match('%.md$') then
    path = path:gsub('README%.md$', 'index.html'):gsub('%.md$', '.html')
    link.target = path .. anchor
  end
  return link
end
