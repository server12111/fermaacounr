from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    phone = State()
    code = State()
    password = State()

