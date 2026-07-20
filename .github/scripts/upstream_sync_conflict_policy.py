import sys


def conflict_action(event_name: str, cache_hit: bool) -> str:
    if event_name == "schedule" and cache_hit:
        return "suppress"
    return "report"


if __name__ == "__main__":
    event_name, cache_hit = sys.argv[1:]
    print(conflict_action(event_name, cache_hit == "true"))
