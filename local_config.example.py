# local_config.example.py — NCP 监控目标配置示例
# 使用: 复制为 local_config.py 并按实际情况修改(local_config.py 不入库,
# 避免泄露内网信息)。未创建 local_config.py 时程序使用内置占位默认值。
NCP_HOST = "localhost"      # NCP 服务地址
NCP_PORT = 8080             # NCP 服务端口
NCP_HTTP_PATH = "/health"   # HTTP 健康检查路径, 留空 "" 跳过检查
TUNNEL_PROC = ""            # 隧道客户端进程名(匹配命令行), 留空 "" 不检测
