"""Authentication: password hashing (argon2) and JWT tokens."""

import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher
from jose import JWTError, jwt

from database import Session, User, get_db

# ================== Argon2 password hashing ==================

_password_hash = PasswordHash((Argon2Hasher(),))

def hash_password(password: str) -> str:
    return _password_hash.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    """校验密码；任何"读不懂的哈希"一律当作不匹配。

    ★ 必须捕获 UnknownHashError：pwdlib 遇到非 argon2 格式的哈希是
      【抛异常】，不是返回 False。不兜住 → 登录接口 500（且完整 traceback
      泄进服务日志），而不是干净的 401「用户名或密码错误」。

    实测依据：kb_bench 有 10 行"实验夹具"直接写库造成的假哈希 ——
      `impl_user_a`/`impl_user_b` = "not-a-real-hash"（test_user_filter_impl.py L126）
      `race_reg_user`/`race_user_1` = "x"（test_bm25_cache_race.py L90）
      `ab_user_88` = "x"（ab_rag_skill_experiment.py L62）
      `repro_user_a`/`repro_user_b` = "x"（repro_mixed_clients_full.py L76）
    登录这 5 个账号原来全部 500。走 /auth/register 的真哈希账号不受影响。
    """
    try:
        return _password_hash.verify(plain, hashed)
    except UnknownHashError:
        return False


# ================== JWT ==================

SECRET_KEY = os.getenv("JWT_SECRET", "change-me-in-production-use-a-real-secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

def create_access_token(user_id: int, username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 无效或已过期",
        )


# ================== FastAPI Dependency ==================

bearer_scheme = HTTPBearer(auto_error=False)

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Return the authenticated user, or raise 401."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录",
        )
    payload = decode_access_token(credentials.credentials)
    user_id = int(payload.get("sub"))
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )
    return user
