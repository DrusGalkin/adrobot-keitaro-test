from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr
from sqlalchemy import JSON, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./adrobot.db"
    keitaro_mode: str = "demo"
    keitaro_url: str = ""
    keitaro_api_key: SecretStr = SecretStr("")


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "records"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


def make_engine(url):
    return create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
