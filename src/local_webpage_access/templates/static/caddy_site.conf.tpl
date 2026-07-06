# Local Webpage Access — Caddy 静态站点配置模板
# 渲染变量：{host_port}、{root}、{site_id}
# 由 lwa 自动生成，请勿手动编辑。

:{host_port} {{
	root * {root}
	file_server
	encode gzip
}}
