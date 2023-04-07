#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import random
from contextvars import ContextVar
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, Sequence, Union

import sqlalchemy as sa
from sqlalchemy import MetaData, Select, create_engine, func
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, AsyncSessionTransaction, async_sessionmaker
from sqlalchemy.orm import declarative_base


class Operate(str, Enum):
    EXECUTE = "execute"
    SCALARS = "scalars"
    SCALAR = "scalar"
    ADD = "add"
    DELETE = "delete"


class TransactionContext:
    transaction: AsyncSessionTransaction

    def __init__(self, database: "AsyncDatabase", session: AsyncSession) -> None:
        self.database = database
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        try:
            self.transaction = await self.session.begin()
        except Exception as e:  # noqa
            self.database.out_transaction()
        return self.session

    async def __aexit__(self, type_, value, traceback):
        try:
            if type_ is None:
                await self.transaction.commit()
            else:
                await self.transaction.rollback()
                raise value
        finally:
            self.database.out_transaction()


class NestedContext:
    transaction: AsyncSessionTransaction

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        try:
            self.transaction = await self.session.begin_nested()
        except Exception as e:  # noqa
            ...
        return self.session

    async def __aexit__(self, type_, value, traceback):
        if type_ is None:
            await self.transaction.commit()
        else:
            await self.transaction.rollback()


class AsyncDatabase:
    _engine: Optional[AsyncEngine]

    def __init__(self):
        self._in_transaction: ContextVar = ContextVar("in_transaction")  # 使用上下文管理变量，管理session和判断是否在事务中
        self._session_context: ContextVar = ContextVar("session")
        self._model = declarative_base(metadata=MetaData())  # 创建数据库模型构造基类

    @property
    def Model(self) -> Any:
        return self._model

    def set_bind(self, url: Union[URL, str], **kwargs) -> None:
        """创建连接"""
        dsn = url.render_as_string(hide_password=False) if isinstance(url, URL) else url
        # 隔离级别不能设置isolation_level="AUTOCOMMIT"，在事务中使用insert添加数据时才能获取到添加后的数据库信息
        self._engine = AsyncEngine(create_engine(dsn, **kwargs))

    def _get(self):
        """内部调用，获取session进行数据库操作"""
        if not self._engine:
            raise ValueError("Database engine is not initialized.")
        try:
            session = self._session_context.get()
        except LookupError:
            session = async_sessionmaker(self._engine, expire_on_commit=False, autoflush=False)()
            self._session_context.set(session)

        if not session:
            session = async_sessionmaker(self._engine, expire_on_commit=False, autoflush=False)()
            self._session_context.set(session)
        return session

    async def _apply(self, op: Operate, obj: Union["Model", Select]):
        """判断是否使用普通提交方式提交数据到数据库"""
        session = self._get()
        res = None
        if op == Operate.ADD:  # update 时调用，add方法不是异步方法不需要进入事件循环
            session.add(obj)
        else:
            res = await session.__getattribute__(op)(obj)
        if self.in_transaction() is False:
            try:
                # commit()会先调用flush()清理缓存，然后提交事务； flush()只清理缓存，不提交事务
                # 不能使用直接使用flush，让session回到连接池只能是commit，rollback，close操作
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise exc
        return res

    async def execute(self, q):  # core执行
        res = await self._apply(Operate.EXECUTE, q)
        return res

    async def scalars(self, q):  # core执行
        res = await self._apply(Operate.SCALARS, q)
        return res

    async def scalar(self, q):  # core执行
        res = await self._apply(Operate.SCALAR, q)
        return res

    async def add(self, m):  # orm执行
        # 这里只对update数据时使用，因为在事务中使用orm添加数据，无法获取到数据对象插入数据库后的信息，必须事务结束后才能获取到。但是更新操作不会。
        # orm和core操作数据库，底层日志都是一样的
        res = await self._apply(Operate.ADD, m)
        return res

    async def delete(self, m):  # orm执行
        await self._apply(Operate.DELETE, m)

    async def create_all(self):
        """创建所有数据表结构"""
        async with self._engine.begin() as conn:
            await conn.run_sync(self._model.metadata.create_all)

    def in_transaction(self) -> bool:
        """还是需要判断是否在事务里，因为使用普通查询还是需要手动提交"""
        try:
            return self._in_transaction.get()
        except LookupError:
            return False

    def transaction(self) -> TransactionContext:
        """创建事务"""
        session = self._get()
        self._in_transaction.set(True)
        return TransactionContext(self, session)  # 返回类，实现上下文管理事务

    def transaction_nested(self) -> NestedContext:
        """嵌套事务"""
        session = self._get()
        self._in_transaction.set(True)
        return NestedContext(session)

    def out_transaction(self):
        """退出事务，将事务标志还原"""
        try:
            self._in_transaction.set(False)
        except LookupError:
            pass

    async def close(self):
        """退出程序释放连接"""
        await self._engine.dispose()
        self._engine.pool.dispose()


db: AsyncDatabase = AsyncDatabase()


class Base(db.Model):
    __abstract__ = True

    id = sa.Column(sa.BigInteger(), primary_key=True, autoincrement=True)
    created_on = sa.Column(sa.DateTime(), default=datetime.now, server_default=func.now(), index=True)
    version = sa.Column(sa.Float(), default=0, server_default=str(random.random()))

    # __mapper_args__ = {"version_id_col": version}  # 验证对数据对象进行操作时是否传入version参数，且修改时是否已经被修改

    def to_dict(self) -> Dict[str, Union[str, None]]:
        return {c.name: getattr(self, c.name, None) for c in self.__table__.columns}

    @classmethod
    async def get(cls, idx):
        return (await db.scalars(sa.select(cls).with_for_update(nowait=True, of=cls).filter_by(id=idx))).first()

    @classmethod
    async def get_by(cls, **kwargs) -> Optional["Base"]:
        res = (await db.scalars(sa.select(cls).with_for_update(nowait=True, of=cls).filter_by(**kwargs))).first()
        return res

    @classmethod
    async def get_all(cls, **kwargs) -> Sequence[Union[sa.Row, sa.RowMapping]]:
        res = (await db.scalars(sa.select(cls).with_for_update(nowait=True, of=cls).filter_by(**kwargs))).all()
        return res

    @classmethod
    async def create(cls, **values) -> "Base":
        res = (await db.scalars(sa.insert(cls).values(**values).returning(cls))).first()
        return res

    async def update(self, **values):
        [setattr(self, field, value) for field, value in values.items()]
        await db.add(self)
        return self

    async def delete(self):
        """Removes the model from the current entity session and mark for deletion."""
        await db.delete(self)
        return self
