#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import random

from sqlalchemy import Column, Unicode, select

from demo.base import Base, db


class User(Base):
    __tablename__ = "users"

    name = Column(Unicode(), default="no_name")


async def create():
    async with db.transaction():
        user = await User.get_by(name="sqlalchemy_data")
        if not user:
            await User.create(name="sqlalchemy_data", version=0)
            return True
    return False


async def update(version):
    async with db.transaction():
        # TODO for update是线程级的悲观锁，实际上是根据查询id索引 + 查询条件对行进行加锁，
        # user = await User.get(2)
        user = (await db.scalars(select(User).where(User.name.like("sqlalchemy%")).order_by(User.id))).first()
        # TODO 使用version进行版本控制，保证唯一性（悲观锁的概念），就像创建用户一样。创建用户也是用了悲观锁的概念。
        if user and user.version != version:
            task_name = asyncio.current_task().get_name()
            await user.update(name=f"sqlalchemy_{task_name}_{str(round(version, 2))}", version=version)
            return True
    return False


async def main():
    db.set_bind(
        "postgresql+asyncpg://postgres:postgres@192.168.88.221:15432/sqlmodel",
        # isolation_level="AUTOCOMMIT",
        echo=True,
    )
    # await db.create_all()

    version = random.uniform(0, 3)
    # await create()
    # await update(version)
    results = await asyncio.gather(
        *[create() for _ in range(2)],
        # *[update(version) for _ in range(30)],
        return_exceptions=True,
    )
    print(results, "\n", "=" * 50, "\n", results.count(True), results.count(True) == 1)


if __name__ == "__main__":
    asyncio.run(main())
