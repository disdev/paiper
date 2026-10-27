"""Private process boundary for one bounded HTTP request, not a user CLI."""

import json
import sys

from paiper.sources.http import SourceError, _request


def main():
    try:
        body = _request(sys.argv[1])
    except SourceError as error:
        sys.stderr.write(
            json.dumps(
                {
                    "error": str(error)[:300],
                    "retry_after": error.retry_after,
                }
            )
        )
        return 1
    sys.stdout.buffer.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
