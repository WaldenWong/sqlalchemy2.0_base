根据sqlalchemy2.0封装数据库模型的应用层


## sqlalchemy2.0底层执行逻辑

1. sqlalchemy2.0 禁用了autobegin。Python中的DBAPI就是这样使用的。为了防止设置为False后隐式开始事务，却忘了调用任何Session.rollback（）、Session.commit（）或Session.close（）方法导致之后session无法使用，所以取消了autobegin参数。

   ~~~python
   # 隐式写法
   plain_engine = AsyncEngine(create_engine("postgresql+asyncpg://postgres:postgres@192.168.88.221:15432/sqlmodel", echo=True))
   session = async_sessionmaker(plain_engine)()
   result = (await session.scalars(...)).all()
   await session.commit()  # 必须使用 commit，rollback，close其中一个方法
   ~~~

2. expire_on_commit参数。默认为True。在每次commit（）之后，所有实例都将过期，因此在完成事务之后的所有属性/对象访问都将从最近的数据库状态加载

3. python对数据库操作有个隐式事务的概念，即使用普通查询，sqlalchemy会自动给查询包到一个事务内进行执行。sqlalchemy2.0也延用了这种方式。

   参考资料：https://www.oddbird.net/2014/06/14/sqlalchemy-postgres-autocommit/ 里面讲述了为什么使用隐式事务，使用AUTOCOMMIT关闭隐式事务，pep249的大致内容（Python 使用dbapi开启隐式事务）。

## 隔离级别

支持以下四种隔离级别：

- `SERIALIZABLE:`

  可串行化。事务隔离级别最严厉，在进行查询时就会对表或行加上共享锁，其他事务对该表将只能进行读操作，而不能进行写操作

- `REPEATABLE READ:`

  可重读。当两个事务同时进行时，其中一个事务修改数据对另一个事务不会造成影响，即使修改的事务已经提交也不会对另一个事务造成影响。在事务中对某条记录修改，会对记录加上行共享锁，直到事务结束才会释放。

- `READ COMMITTED: ` **（default）**

  读取提交内容。只有在事务提交后，才会对另一个事务产生影响，并且在对表进行修改时，会对表数据行加上行共享锁

- `READ UNCOMMITTED: `

  读取未提交内容。当两个事务同时进行时，即使事务没有提交，所做的修改也会对事务内的查询做出影响，这种级别显然很不安全。但是在表对某行进行修改时，会对该行加上行共享锁

可以使用 `session.connection().execute("SET TRANSACTION ISOLATION LEVEL ...")` 方法来设置隔离级别，其中 `...` 部分应该替换为上述四种隔离级别之一。例如，设置隔离级别为 `READ COMMITTED` 的示例代码如下：

~~~python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine('postgresql://user:password@localhost/mydatabase')
Session = sessionmaker(bind=engine)
session = Session()

session.connection().execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
~~~

需要注意的是，不同的数据库支持的隔离级别可能有所不同，具体应该参考数据库的文档。

~~~python
注意：
    AUTOCOMMIT 不是隔离级别，它是一种事务模式。在 AUTOCOMMIT 模式下，每个 SQL 语句都会自动提交事务，而不需要显式地调用 COMMIT 方法。这意味着每个 SQL 语句都是一个单独的事务，它们之间没有隔离级别的概念。
    在 SQLAlchemy 中，可以使用 session.autocommit 属性来设置事务模式。如果将其设置为 True，则表示使用 AUTOCOMMIT 模式，否则表示使用默认的事务模式。

需要注意的是，AUTOCOMMIT 模式可能会导致一些问题，例如：
    脏读：如果一个事务修改了数据但还没有提交，另一个事务可能会读取到未提交的数据，导致脏读问题。
    不可重复读：如果一个事务多次读取同一数据，但在读取过程中另一个事务修改了该数据并提交，那么第一个事务得到的结果就会不一致，导致不可重复读问题。
    幻读：如果一个事务多次读取同一数据，但在读取过程中另一个事务插入了新的数据并提交，那么第一个事务得到的结果就会不一致，导致幻读问题。
    因此，建议在使用 SQLAlchemy 进行数据库操作时，不要使用 AUTOCOMMIT 模式，而是使用默认的事务模式，并根据需要设置合适的隔离级别来保证数据的一致性
设置了AUTOCOMMIT后sql日志如下
"""
设置AUTOCOMMIT，结束输出：
...
2023-04-04 10:34:29,539 INFO sqlalchemy.engine.Engine ROLLBACK using DBAPI connection.rollback(), DBAPI should ignore due to autocommit mode

如果不设置AUTOCOMMIT，结束输出：
...
2023-04-04 10:34:29,539 INFO sqlalchemy.engine.Engine ROLLBACK
"""
所以如果需要优化性能，engine的设计可以是创建一个ReaderEngine()用于读，一个WriterEngine()用于写，但查询并更新的操作必须在一个事务内。

~~~

## 解决并发重复操作问题

> “在相对高并发对用户账户进行扣费”时会出现扣减额不正确的情况，例如**每笔订单0.1元，同时发起100笔，理应扣费10元，但系统实际扣费小于10元**。

### 线程

执行原理

> session 以 **线程** 非并发的方式使用的，这通常意味着一次只能在一个线程中使用。
>
> 如果实际上有多个线程参与同一任务，那么您可以考虑在这些线程之间共享会话及其对象；然而，在这种极不寻常的情况下，应用程序需要确保实现正确的锁定方案，这样就不会并发访问会话或其状态。对于这种情况，更常见的方法是为每个并发线程维护一个会话，但将对象从一个会话复制到另一个会话中，通常使用Session.merge（）方法将对象的状态复制到不同会话本地的新对象中

解决方法一

> 使用for update，for update 仅适用于InnoDB，且必须在事务处理模块（BEGIN/COMMIT）中才能生效，代码如下
>
> ~~~python
> engine = create_engine("sqlite:///example.db")
> session = sessionmaker(engine, expire_on_commit=False)()
>
> # 线程并发执行
> def find_data(id):
>     with session.begin():
>         user = session.query(MyModel).with_for_update(nowait=True, of=User).filter_by(id=id).first()
>         if user:
>         	user.age = 30
>     return data
> if __name__ == '__main__':
>     with ThreadPoolExecutor(max_workers=10) as executor:
>         futures = [executor.submit(find_data, id) for id in range(1, 20)]
>         results = [future.result() for future in futures]
> ~~~

解决方法二

> 设置隔离级别 SERIALIZABLE（会降低线程并发性能）
>
> ~~~python
> engine = create_engine("sqlite:///example.db"，isolation_level="SERIALIZABLE")
> ~~~

解决方法三

> 使用version版本号判断每次是否更新同一条数据
>
> ~~~python
> import asyncio
> from concurrent.futures import ThreadPoolExecutor
> from datetime import datetime
>
> from sqlalchemy import create_engine, MetaData, func, select
> from sqlalchemy.orm import sessionmaker, declarative_base
> import sqlalchemy as sa
>
> Model = declarative_base(metadata=MetaData())
>
>
> class User(Model):
>     __tablename__ = 'users'
>
>     id = sa.Column(sa.BigInteger(), primary_key=True, autoincrement=True)
>     name = sa.Column(sa.String(), default='no_name')
>     created_on = sa.Column(sa.DateTime(), default=datetime.now, server_default=func.now(), index=True)
>     version = sa.Column(sa.Integer(), default=0)
>     # __mapper_args__ = {"version_id_col": version}
>
> """
> version_id_col 是用于指定版本号列的参数。版本号列是用于实现乐观并发控制的一种方式，它可以用来检测并发修改冲突。当一个对象被修改时，版本号会自动递增，如果在保存对象时发现版本号与数据库中的不一致，就会抛出 StaleDataError 异常，提示用户数据已经被修改过了。
> """
>
>
> def create(engine):
>     session = sessionmaker(engine, expire_on_commit=False)()
>     user = User(name='123123')
>     session.add(user)
>     session.commit()
>     return user.id
>
>
> def update(engine, version):
>     session = sessionmaker(engine, expire_on_commit=False)()
>     try:
>         user = session.scalars(select(User).with_for_update(nowait=True, of=User).filter_by(name='123123')).first()
>         if user and user.version != version:
>             user.version = version
>             session.commit()
>             return True
>         return False
>     finally:
>         session.close()
>
>
> async def main():
>     engine = create_engine('postgresql://postgres:postgres@192.168.88.221:15432/sqlmodel', echo=True)
>     # User.metadata.create_all(engine)
>     # uid = create(engine)
>     # 线程并发执行
>     with ThreadPoolExecutor(max_workers=10) as executor:
>         version = random.randint(0, 50)
>         futures = [executor.submit(update, engine, version) for _ in range(1, 10)]
>         results = [future.result() for future in futures]
>         print(results, "\n", "=" * 50, "\n", results.count(True), results.count(True) == 1)
>
>
> if __name__ == '__main__':
>     asyncio.run(main())
> ~~~

### 协程

> 使用for update不会每次都会生效，由于线程与协程资源会共享，所以会出现有部分协程任务会触发设置的for update悲观锁，所以协程避免并发重复更新问题目前我只知道使用随机version控制(虽然是个不可取的办法，但是确实能控制协程并发重复更新问题)

~~~python
import asyncio
import random
from datetime import datetime

from sqlalchemy import create_engine, select, Unicode, Column, Integer, BigInteger, DateTime, func, MetaData, insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

Model = declarative_base(metadata=MetaData())


class User(Model):
    __tablename__ = 'users'

    id = Column(Integer(), primary_key=True, autoincrement=True)
    name = Column(Unicode(), default='no_name')
    created_on = Column(DateTime(), default=datetime.now, server_default=func.now(), index=True)
    version = Column(Integer(), default=0)

    __mapper_args__ = {'version_id_col': version}


async def create(engine):
    session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()
    async with session.transaction():
        user = (
            await session.scalars(select(User).filter_by(name='test_data').with_for_update(nowait=True, of=User))
        ).first()
        if not user:
            await session.execute(insert(User).values(name='test_data'))
            return True
        return False


async def update_user(engine, user_id, version):
    session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)()
    async with session.begin():
        user = (await session.scalars(select(User).filter_by(id=user_id).with_for_update(nowait=True, of=User))).first()
        if user and user.version != version:
            task_name = asyncio.current_task().get_name()
            user.name = f"sqlalchemy_{task_name}"
            user.version = version
            return True
    return False



async def main():
    engine = AsyncEngine(
        create_engine('postgresql+asyncpg://postgres:postgres@192.168.88.221:15432/sqlmodel', echo=True)
    )
    version = random.randint(0, 20)
    results = await asyncio.gather(*[update_user(engine, 1, version) for _ in range(10)], return_exceptions=True)
    print(results, "\n", "=" * 50, "\n", results.count(True), results.count(True) == 1)


if __name__ == '__main__':
    asyncio.run(main())
~~~

## 数据基类设计

> 基类需要完成如下可被调用的操作，创建连接、创建事务、创建所有表结构、能够使用sqlalchemy中core或orm方式执行sql或操作数据库对象、释放数据库连接
>
> 操作数据库**orm比core操作要快**，100个并发更新操作orm比core快0.05-0.1左右的时间戳，创建操作orm比core快0.03-0.05左右的时间戳

~~~python
class Operate(str, Enum):
    EXECUTE = "execute"
    SCALARS = "scalars"
    SCALAR = "scalar"
    ADD = "add"
    DELETE = "delete"


class TransactionContext:
    """事务"""
    transaction: AsyncSessionTransaction

    def __init__(self, database: "AsyncDatabase", session: AsyncSession) -> None:
        self.database = database
        self.session = session

    async def __aenter__(self) -> AsyncSession:  # 上下文开始
        try:
            self.transaction = await self.session.begin()
        except Exception as e:  # noqa
            self.database.out_transaction()
        return self.session

    async def __aexit__(self, type_, value, traceback): # 上下文结束
        try:
            if type_ is None:
                await self.transaction.commit()
            else:
                await self.transaction.rollback()
                raise value
        finally:
            self.database.out_transaction() # 事务退出，标志位初始化


class NestedContext:
    """嵌套事务"""
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
        self._in_transaction: ContextVar = ContextVar("in_transaction") # 使用上下文管理变量，管理session和判断是否在事务中
        self._session_context: ContextVar = ContextVar("session")
        self._model = declarative_base(metadata=MetaData()) # 创建数据库模型构造基类

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
        """判断是否使用普通提交方式提交数据到数据库 """
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

    async def execute(self, q): # core执行
        res = await self._apply(Operate.EXECUTE, q)
        return res

    async def scalars(self, q): # core执行
        res = await self._apply(Operate.SCALARS, q)
        return res

    async def scalar(self, q): # core执行
        res = await self._apply(Operate.SCALAR, q)
        return res

    async def add(self, m): # orm执行
        # 这里只对update数据时使用，因为在事务中使用orm添加数据，无法获取到数据对象插入数据库后的信息，必须事务结束后才能获取到。但是更新操作不会。
        # orm和core操作数据库，底层日志都是一样的
        res = await self._apply(Operate.ADD, m)
        return res

    async def delete(self, m): # orm执行
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
        return TransactionContext(self, session) # 返回类，实现上下文管理事务

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
~~~

## 数据应用层设计

~~~python
db: AsyncDatabase = AsyncDatabase()


class Base(db.Model):
    __abstract__ = True

    id = sa.Column(sa.BigInteger(), primary_key=True, autoincrement=True)
    created_on = sa.Column(sa.DateTime(), default=datetime.now, server_default=func.now(), index=True)
    version = sa.Column(sa.Float(), default=0, server_default=str(random.random()))

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
        # returning可以获取插入数据后的信息
        res = (await db.scalars(sa.insert(cls).values(**values).returning(cls))).first()
        return res

    async def delete(self):
        await db.delete(self)
        return self

    async def update(self, **values):
        # 更新操作要在事务中进行
        [setattr(self, field, value) for field, value in values.items()]
        await db.add(self)
        return self

~~~
