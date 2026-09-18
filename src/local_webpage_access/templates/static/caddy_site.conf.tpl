# Local Webpage Access — Caddy 静态站点配置模板
# 渲染变量：{host_port}、{root}、{site_id}、{rate_limit_block}、{access_log}、{bind_directive}
# （IMP-005/IMP-028；{bind_directive} 在 instanceBindHost 收敛时为 bind <ip>，默认空）
# {rate_limit_block} 在未启用限流 / 能力不可用时为空，留下一行空行（Caddyfile 忽略）。
# {access_log} 把直连端口的访问写入共享 access log，供浏览量按端口归属（无别名的静态站点）。
# 由 lwa 自动生成，请勿手动编辑。

:{host_port} {{
{bind_directive}	root * {root}
	file_server
	encode gzip
{rate_limit_block}
	log {{
		output file {access_log} {{
			roll_size 10mb
			roll_keep 3
		}}
		format json
	}}
}}
