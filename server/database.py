"""SQLAlchemy ORM models and database session."""

from pathlib import Path

from sqlalchemy import (create_engine, String, Text, DateTime, ForeignKey,
                        Float, Boolean, Integer)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session
from datetime import datetime, timezone

# 锚定项目根目录，任何 cwd 下运行都写同一个 app.db（避免子目录运行静默建新库）
DATABASE_URL = f"sqlite:///{(Path(__file__).resolve().parent / 'app.db').as_posix()}"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


# ===================== User =====================

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


# ===================== Conversation =====================

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(100), default="新对话")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        order_by="Message.created_at"
    )


# ===================== Message =====================

class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # "user" or "assistant"
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    # 级联删除：删会话 → 级联删 messages → 这里再级联删 message_sources。
    # ★ 不依赖 SQLite 的外键级联：SQLite 默认不开 PRAGMA foreign_keys，
    #   所以必须由 ORM 这一层兜住，否则历史里会留下一堆孤儿来源行。
    sources: Mapped[list["MessageSource"]] = relationship(
        cascade="all, delete-orphan",
        order_by="MessageSource.ordinal",
    )


class MessageSource(Base):
    """助手消息的【检索来源】—— 引用溯源用。

    ★ 单独建表，而不是给 messages 加列：create_all() 会自动建【新表】，
      而给已存在的表加列要手写 ALTER（见 _migrate）。所以这样零迁移风险，
      也完全不动 messages 的既有结构。

    一行 = 一条来源（文件名 / 章节路径 / 相关度 / 正文片段），
    ordinal 就是回答正文里的引用序号。
    """
    __tablename__ = "message_sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)   # 来源对象的 JSON
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )


# ===================== Knowledge Document =====================

class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    # 公共库标记：True = 所有用户都能检索到这份文档（例如学校统一的评定办法）。
    # 🔴 刻意【不】做成入库参数 —— 入库路径无权决定"这份文档对所有人可见"，
    #    否则等于把"公开发布"能力交给调用方（乃至 LLM）。只由管理端显式调用
    #    set_document_shared() 设置；默认 False 是"失败关闭"方向。
    is_shared: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True
    )

    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="processing",
        nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan"
    )

# ===================== Knowledge Chunk =====================

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id"),
        nullable=False,
        index=True
    )

    chunk_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False
    )

    module: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )

    sub_module: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True
    )

    score_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True
    )

    base_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True
    )

    section_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    score_rules: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    keywords: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    document: Mapped["KnowledgeDocument"] = relationship(
        back_populates="chunks"
    )
# ===================== Session Factory =====================

def get_db():
    """Yield a database session (use with FastAPI Depends)."""
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()


def new_session() -> Session:
    """绑定 engine 的会话工厂（供非 FastAPI-Depends 的场景使用）。

    ★ 为什么必须要有它：`Session` 是 SQLAlchemy 的【类】，不是工厂 ——
      它自身【不带 bind】，直接 `Session()` 再执行查询会抛
      `UnboundExecutionError: Could not locate a bind configured on ...`。

      实测：`conversation_context._load_raw` 的默认 fallback 原来写成
      `session_factory or Session`，于是生产路径 `POST /chat` 每次都 500；
      而 110/110 的测试因为各自注入了 `session_factory`，
      【从未走到这条 fallback】—— 测试全绿、生产必崩。

    凡是要"自己开一个 session"的模块都必须用它，不要再各写各的
      `Session(engine)` —— 否则 engine 的来源散成多份。
    这与 §19 是同一型坑：config.py 已把"路径"收敛成唯一来源，engine 同理。
    """
    return Session(engine)


def init_db():
    """Create all tables. Call once at startup."""
    Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """补齐旧库缺失的列 —— create_all() 不会 ALTER 已存在的表。

    幂等：重复调用无副作用。处理两列：
      · knowledge_documents.user_id   —— 用户隔离所必需（reranked 的元数据过滤靠它取 doc_ids）
      · knowledge_documents.is_shared —— 公共库标记（is_shared=1 的文档所有用户都查得到）
    """
    with engine.begin() as conn:
        existing = {row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info(knowledge_documents)")}
        if not existing:
            return

        if "user_id" not in existing:
            # SQLite 的 ADD COLUMN 不能加 NOT NULL（除非带默认值）。
            # 旧行留 NULL —— NULL 不匹配任何 user_id 过滤条件，也【不是】公共（is_shared 默认 0），
            # 于是"历史遗留文档对所有人不可见"。这是安全的失败方向：
            # 宁可查不到，不可串档；归属/公开与否必须由管理端显式决定。
            conn.exec_driver_sql(
                "ALTER TABLE knowledge_documents ADD COLUMN user_id INTEGER")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_user_id "
                "ON knowledge_documents (user_id)")

        if "is_shared" not in existing:
            # 这一列可以带默认值加 NOT NULL：NOT NULL DEFAULT 0 => 旧行一律【私有】，
            # 不会因为迁移而突然对所有人公开（失败关闭）。
            conn.exec_driver_sql(
                "ALTER TABLE knowledge_documents ADD COLUMN is_shared BOOLEAN NOT NULL DEFAULT 0")
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_documents_is_shared "
                "ON knowledge_documents (is_shared)")


def set_document_shared(document_id: int, shared: bool = True) -> bool:
    """把一份文档设为公共（或取消公共）。**仅限管理端/回填使用。**

    ⚠️ 共享范围一变，【所有】用户的 BM25 语料都变了 ——
    调用方必须同时调用 langchain_project.invalidate_all_bm25()，
    否则各用户缓存里仍是旧的文档集合。
    """
    with Session(engine) as s:
        doc = s.get(KnowledgeDocument, document_id)
        if doc is None:
            return False
        doc.is_shared = bool(shared)
        s.commit()
        return True
