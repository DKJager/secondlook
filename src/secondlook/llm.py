import json
import urllib.request
import urllib.parse
import urllib.error
from typing import List, Dict, Any, Optional


class SecondLookError(RuntimeError):
    """Error raised when an LLM request fails or returns malformed data."""
    pass


def chat(
    base_url: str,
    key: str,
    model: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> Dict[str, Any]:
    """Send a chat completion request.

    Args:
        base_url: Base URL of the OpenAI‑compatible API.
        key: API key used for Bearer authentication.
        model: Model identifier to query.
        messages: List of message dicts for the chat request.
        max_tokens: Maximum number of tokens to generate.
        temperature: Sampling temperature.
        timeout: Request timeout in seconds.
        opener: Optional urllib opener for injection; defaults to the global opener.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        SecondLookError: On HTTP errors, non‑2xx status, or JSON parsing failures.
    """
    url = urllib.parse.urljoin(base_url, "/v1/chat/completions")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode('utf-8')
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        if opener is not None:
            response = opener.open(request, timeout=timeout)
        else:
            response = urllib.request.urlopen(request, timeout=timeout)
        status = response.getcode()
        response_body = response.read().decode('utf-8')
        if status != 200:
            raise SecondLookError(
                f"HTTP {status}: {response_body[:200] if response_body else 'no response body'}"
            )
        return json.loads(response_body)
    except json.JSONDecodeError as e:
        raise SecondLookError(f"JSON parse failure: {e}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        raise SecondLookError(f"HTTP {e.code}: {error_body[:200]}")
    except urllib.error.URLError as e:
        raise SecondLookError(f"URL error: {e.reason}")
    except OSError as e:
        raise SecondLookError(f"Network error: {e}")
    except Exception as e:
        raise SecondLookError(f"Unexpected error: {e}")
