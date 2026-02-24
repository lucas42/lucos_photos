import os
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

database_url = URL.create(
    drivername="postgresql",
    username=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    host="postgres",
    port=5432,
    database="photos",
)

engine = create_engine(database_url)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass
