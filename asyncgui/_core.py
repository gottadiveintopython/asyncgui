__all__ = (
    'start', 'sleep_forever', 'Event', 'Task', 'TaskState',
    'get_current_task', 'get_step_coro', 'aclosing', 'Awaitable_or_Task',
    'raw_start', 'cancel_protection',
)

import itertools
import types
import typing
from inspect import (
    getcoroutinestate, CORO_CLOSED, CORO_RUNNING, isawaitable,
)
import enum
from contextlib import asynccontextmanager

from asyncgui.exceptions import InvalidStateError


class TaskState(enum.Flag):
    CREATED = enum.auto()
    '''CORO_CREATED'''

    STARTED = enum.auto()
    '''CORO_RUNNING or CORO_SUSPENDED'''

    CANCELLED = enum.auto()
    '''CORO_CLOSED by 'Task.cancel()' or an uncaught exception'''

    DONE = enum.auto()
    '''CORO_CLOSED (coroutine was completed)'''

    ENDED = CANCELLED | DONE


class Task:
    '''
    Task
    ====
    '''

    __slots__ = (
        'name', '_uid', '_root_coro', '_state', '_result', '_event',
        '_cancel_called', 'userdata', '_exception', '_suppresses_exception',
        '_cancel_protection',
    )

    _uid_iter = itertools.count()

    def __init__(self, awaitable, *, name='', userdata=None):
        if not isawaitable(awaitable):
            raise ValueError(str(awaitable) + " is not awaitable.")
        self._uid = next(self._uid_iter)
        self.name: str = name
        self.userdata = userdata
        self._cancel_protection = 0
        self._root_coro = self._wrapper(awaitable)
        self._state = TaskState.CREATED
        self._event = Event()
        self._cancel_called = False
        self._exception = None
        self._suppresses_exception = False

    def __str__(self):
        return f'Task(uid={self._uid}, name={self.name!r})'

    @property
    def uid(self) -> int:
        return self._uid

    @property
    def root_coro(self) -> typing.Coroutine:
        return self._root_coro

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def done(self) -> bool:
        return self._state is TaskState.DONE

    @property
    def cancelled(self) -> bool:
        return self._state is TaskState.CANCELLED

    @property
    def result(self):
        '''Equivalent of asyncio.Future.result()'''
        state = self._state
        if state is TaskState.DONE:
            return self._result
        elif state is TaskState.CANCELLED:
            raise InvalidStateError(f"{self} was cancelled")
        else:
            raise InvalidStateError(f"Result of {self} is not ready")

    async def _wrapper(self, awaitable):
        try:
            self._state = TaskState.STARTED
            self._result = await awaitable
        except Exception as e:
            self._state = TaskState.CANCELLED
            if self._suppresses_exception:
                self._exception = e
            else:
                raise
        except:  # noqa: E722
            self._state = TaskState.CANCELLED
            raise
        else:
            self._state = TaskState.DONE
        finally:
            self._event.set(self)

    def cancel(self):
        '''Cancel the task as soon as possible'''
        self._cancel_called = True
        if self._is_cancellable:
            self._actual_cancel()

    def _actual_cancel(self):
        self._root_coro.close()

    # give 'cancel()' an alias so that we can cancel tasks just like we close
    # coroutines.
    close = cancel

    @property
    def _is_cancellable(self) -> bool:
        '''
        Indicates whether the task can be immediately cancellable.
        '''
        return (not self._cancel_protection) and \
            getcoroutinestate(self._root_coro) != CORO_RUNNING

    def _step_coro(self, *args, **kwargs):
        coro = self._root_coro
        try:
            if getcoroutinestate(coro) != CORO_CLOSED:
                coro.send((args, kwargs, ))(self._step_coro)
        except StopIteration:
            pass
        else:
            if self._cancel_called and self._is_cancellable:
                self._actual_cancel()


@asynccontextmanager
async def cancel_protection():
    '''
    (experimental) Async context manager that protects the code-block from
    cancellation even if it contains 'await'.

    .. code-block:: python

       async with asyncgui.cancel_protection():
           await something1()
           await something2()
    '''
    task = await get_current_task()
    task._cancel_protection += 1
    try:
        yield
    finally:
        task._cancel_protection -= 1


Awaitable_or_Task = typing.Union[typing.Awaitable, Task]


def start(awaitable_or_task: Awaitable_or_Task) -> Task:
    '''Starts a asyncgui-flavored awaitable or a Task.

    If the argument is a Task, itself will be returned. If it's an awaitable,
    it will be wrapped in a Task, and the Task will be returned.
    '''
    if isawaitable(awaitable_or_task):
        task = Task(awaitable_or_task)
    elif isinstance(awaitable_or_task, Task):
        task = awaitable_or_task
        if task._state is not TaskState.CREATED:
            raise ValueError(f"{task} was already started")
    else:
        raise ValueError("Argument must be either of a Task or an awaitable.")

    coro = task._root_coro
    try:
        coro.send(None)(task._step_coro)
    except StopIteration:
        pass
    else:
        if task._cancel_called and task._is_cancellable:
            task._actual_cancel()

    return task


def raw_start(coro: typing.Coroutine) -> typing.Coroutine:
    '''Starts a asyncgui-flavored coroutine.

    Unlike ``start()``, the argument will not be wrapped in a Task, and will
    not be validated at all.
    '''
    def step_coro(*args, **kwargs):
        try:
            if getcoroutinestate(coro) != CORO_CLOSED:
                coro.send((args, kwargs, ))(step_coro)
        except StopIteration:
            pass

    step_coro._task = None

    try:
        coro.send(None)(step_coro)
    except StopIteration:
        pass

    return coro


@types.coroutine
def sleep_forever():
    yield lambda step_coro: None


class Event:
    '''
    Event
    =====

    Similar to 'trio.Event'.
    '''
    __slots__ = ('_value', '_flag', '_step_coro_list', )

    def __init__(self):
        self._value = None
        self._flag = False
        self._step_coro_list = []

    def is_set(self):
        return self._flag

    def set(self, *args, **kwargs):
        if self._flag:
            return
        self._flag = True
        self._value = (args, kwargs)
        step_coro_list = self._step_coro_list
        self._step_coro_list = []
        for step_coro in step_coro_list:
            step_coro(*args, **kwargs)

    def clear(self, *args, **kwargs):
        self._flag = False

    @types.coroutine
    def wait(self):
        if self._flag:
            yield lambda step_coro: step_coro()
            return self._value
        else:
            return (yield self._step_coro_list.append)

    def add_callback(self, callback):
        '''(internal)'''
        if self._flag:
            args, kwargs = self._value
            callback(*args, **kwargs)
        else:
            self._step_coro_list.append(callback)


@types.coroutine
def get_step_coro():
    '''Returns the task-runner'''
    return (yield lambda step_coro: step_coro(step_coro))[0][0]


async def get_current_task():
    '''Returns the task currently running. None if no Task is associated.'''
    return getattr(await get_step_coro(), '__self__', None)


@asynccontextmanager
async def aclosing(aiter):
    '''async version of 'contextlib.closing()'
    '''
    try:
        yield aiter
    finally:
        await aiter.aclose()
