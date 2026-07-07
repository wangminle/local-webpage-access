# Local Webpage Access — Caddy 静态站点配置模板
# 渲染变量：{host_port}、{root}、{site_id}、{rate_limit_block}（IMP-005）
# {rate_limit_block} 在未启用限流 / 能力不可用时为空，留下一行空行（Caddyfile 忽略）。
# 由 lwa 自动生成，请勿手动编辑。

:{host_port} {{
	root * {root}
	file_server
	encode gzip
{rate_limit_block}
}}
