from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    method = State()
    phone = State()
    code = State()
    password = State()
    tdata = State()



class AddChat(StatesGroup):
    reference = State()


class EngagementOrder(StatesGroup):
    link = State()
    reaction = State()
    prerequisite_links = State()
    cooldown = State()
    quantity = State()
