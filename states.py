from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    phone = State()
    code = State()
    password = State()



class AddChat(StatesGroup):
    reference = State()


class EngagementOrder(StatesGroup):
    link = State()
    reaction = State()
    prerequisite_links = State()
    cooldown = State()
    quantity = State()
