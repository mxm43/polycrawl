# Docker Secrets

该目录用于存放 Docker Secrets 文件（`postgres_password.txt`、`redis_password.txt`）。

## 使用方式

```bash
# 1. 写入密码（不要加换行符）
echo -n 'your_strong_password_here' > secrets/postgres_password.txt
echo -n 'your_strong_password_here' > secrets/redis_password.txt

# 2. 启动服务
docker compose up -d

# 3. （可选）启动后删除原文——密码已注入容器，原文不再需要
rm secrets/postgres_password.txt secrets/redis_password.txt

# 4. 验证
docker compose ps
```

## 注意事项

- 该目录已被 `.gitignore` 排除，不会提交到 Git
- 如果不需要 Docker Secrets（仅用 `.env`），忽略此目录即可
- 如需重置密码：修改 `.txt` 文件 → `docker compose up -d` 重建容器
