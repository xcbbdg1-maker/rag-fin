"""初始化三个演示账号。首次部署跑一次：python seed.py
   登录后请立刻用 /api/password 或页面上的改密功能修改默认密码。
"""
import db
import security

ACCOUNTS = [
    ("admin",    "admin123",    ["admin"]),
    ("finance",  "finance123",  ["finance"]),
    ("employee", "employee123", ["employee"]),
]


def main():
    db.init_db()
    for username, password, roles in ACCOUNTS:
        if db.get_user(username):
            print(f"跳过已存在账号：{username}")
            continue
        db.create_user(username, security.hash_password(password), roles)
        print(f"已创建：{username} / {password}   roles={roles}")
    print("\n⚠️  这些是默认密码，登录后请立即修改！")


if __name__ == "__main__":
    main()
