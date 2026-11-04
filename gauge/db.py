"""Database tables: runs, results and labels."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, or_, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    """One benchmark run: a set of tasks against a set of setups."""
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    git_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_set: Mapped[str] = mapped_column(String(200))
    setups: Mapped[list] = mapped_column(JSON, default=list)
    repeats: Mapped[int] = mapped_column(Integer, default=1)
    mode: Mapped[str] = mapped_column(String(20), default="full")  # full, dev or ci
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    stopped_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    results: Mapped[list["Result"]] = relationship(back_populates="run")


class Result(Base):
    """One answer from one setup to one task (one repeat)."""
    __tablename__ = "results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    setup_name: Mapped[str] = mapped_column(String(100), index=True)
    task_id: Mapped[str] = mapped_column(String(100), index=True)
    category: Mapped[str] = mapped_column(String(30))
    repeat_index: Mapped[int] = mapped_column(Integer)
    output_text: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    check_type: Mapped[str] = mapped_column(String(20))
    judge_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 1 = judge says correct
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_s3_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Same setup + task + prompt + repeat gives the same key, so reruns can reuse answers.
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)

    run: Mapped[Run] = relationship(back_populates="results")


class Label(Base):
    """A human check of one judged result, used to measure how far to trust the judge."""
    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("results.id"), unique=True)
    human_score: Mapped[int] = mapped_column(Integer)  # 1 = correct, 0 = wrong
    labeled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def make_engine(database_url: str):
    return create_engine(database_url, future=True, pool_pre_ping=True)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def find_cached(session: Session, cache_key: str) -> Result | None:
    """Latest usable result with this cache key, if any.

    Calls that failed because of throttling or an outage are not cached, so a rerun retries them.
    A model that looped until the tool step limit, or invented an invalid tool name, is a real result and is cached.
    """
    stmt = (
        select(Result)
        .where(Result.cache_key == cache_key,
               or_(Result.error.is_(None), Result.error.like("hit max tool steps%"),
                   Result.error.like("%toolUse.name%")))
        .order_by(Result.id.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()
