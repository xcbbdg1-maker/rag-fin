"""密码哈希（bcrypt）与 JWT 令牌。角色不放进前端可改的地方，只从服务端签发的令牌里取。"""
from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import jwt, JWTError
from config import SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
_ALGO = "HS256"


def hash_password(p: str) -> str:
    return _pwd.hash(p)


def verify_password(p: str, h: str) -> bool:
    return _pwd.verify(p, h)


def create_token(username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "exp": exp}, SECRET_KEY, algorithm=_ALGO)


def decode_token(token: str):
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=[_ALGO])
        return data.get("sub")
    except JWTError:
        return None
