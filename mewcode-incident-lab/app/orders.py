"""Order summary logic. The empty-result bug is deliberately retained for the lab."""


def summarize(amounts):
    return {
        "count": len(amounts),
        "total_cents": sum(amounts),
        "average_cents": sum(amounts) / len(amounts),
    }
