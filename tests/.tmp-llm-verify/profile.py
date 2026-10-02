"""用户配置读取。"""


def get_profile(user):
    """返回用户的 profile。"""
    return user["profile"]


def get_tier(user):
    """返回用户的等级。

    需求：user 没有 profile 时应该返回 "GUEST"，不能抛异常。
    """
    profile = get_profile(user)
    return profile["tier"]          # ← BUG: profile 可能是 None
