"""PayPal sandbox integration for Second Look.

Creates and captures orders using PayPal's v2 Checkout API in the sandbox
environment, handling authentication and request IDs.
"""

import base64
import json
import os
import uuid
import urllib.request
import urllib.parse
from typing import Dict, Any, Optional


class SecondLookError(RuntimeError):
    """Error raised when PayPal API calls fail."""
    pass


class PayPalSandbox:
    """Client for PayPal sandbox operations.

    Handles OAuth token retrieval, order creation, and order capture using
    urllib and standard library only.
    """

    def __init__(
        self,
        client_id: str,
        secret: str,
        base_url: str = "https://api-m.sandbox.paypal.com",
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> None:
        """Initialize the sandbox client.

        Args:
            client_id: PayPal REST API client identifier.
            secret: PayPal REST API secret.
            base_url: Base URL for API calls; defaults to sandbox endpoint.
            opener: Optional urllib opener for injection; defaults to the global opener.
        """
        self.client_id = client_id
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.opener = opener if opener is not None else urllib.request.build_opener()
        self._access_token: Optional[str] = None

    def _http_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        data: Optional[bytes] = None,
    ) -> urllib.response.addinfourl:
        """Perform an HTTP request using the configured opener."""
        request = urllib.request.Request(url, data=data, method=method)
        for name, value in headers.items():
            request.add_header(name, value)
        return self.opener.open(request)

    def _ensure_token(self) -> str:
        """Return a valid access token, fetching it if necessary."""
        if self._access_token is None:
            self._access_token = self.get_token()
        return self._access_token

    def get_token(self) -> str:
        """Obtain an OAuth2 access token from PayPal.

        Returns:
            Bearer token string.

        Raises:
            SecondLookError: On HTTP or parsing failure.
        """
        token_url = f"{self.base_url}/v1/oauth2/token"
        body_dict = {"grant_type": "client_credentials"}
        body_bytes = urllib.parse.urlencode(body_dict).encode("utf-8")
        auth_string = f"{self.client_id}:{self.secret}"
        auth_header = base64.b64encode(auth_string.encode("utf-8")).decode("ascii")

        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            response = self._http_request("POST", token_url, headers, data=body_bytes)
            status = response.getcode()
            raw = response.read().decode("utf-8")
        except Exception as exc:
            raise SecondLookError(f"Token request failed: {exc}") from exc

        if not (200 <= status < 300):
            raise SecondLookError(f"Token request error ({status}): {raw}")

        try:
            payload = json.loads(raw)
            return payload["access_token"]
        except Exception as exc:
            raise SecondLookError(f"Invalid token response: {raw}") from exc

    def create_order(self, purchase: Dict[str, str], verdict_summary: str) -> Dict[str, Any]:
        """Create a PayPal order with the verdict summary embedded.

        Args:
            purchase: Mapping with "item", "price", "currency", and other fields.
            verdict_summary: Text summarizing the crowd decision; truncated as needed.

        Returns:
            Dictionary containing order "id", "status", and "approve_url".

        Raises:
            SecondLookError: On API errors.
        """
        order_url = f"{self.base_url}/v2/checkout/orders"
        token = self._ensure_token()
        request_id = str(uuid.uuid4())

        # Truncate description and custom_id to 127 characters as required.
        description = verdict_summary[:127]

        # Build a simple custom_id that contains the prefix and truncated summary.
        max_custom_len = 127 - len("secondlook:")
        custom_id = "secondlook:" + verdict_summary[:max_custom_len]

        # Application context URLs.
        return_url = os.getenv(
            "SECONDLOOK_RETURN_URL", "https://example.com/return"
        )
        cancel_url = os.getenv(
            "SECONDLOOK_RETURN_URL", "https://example.com/return"
        )

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {
                        "currency_code": purchase["currency"],
                        "value": purchase["price"],
                    },
                    "description": description,
                    "custom_id": custom_id,
                    "items": [
                        {
                            "name": purchase["item"],
                            "unit_amount": {
                                "currency_code": purchase["currency"],
                                "value": purchase["price"],
                            },
                            "quantity": "1",
                        }
                    ],
                }
            ],
            "application_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "PayPal-Request-Id": request_id,
        }

        try:
            response = self._http_request(
                "POST",
                order_url,
                headers,
                data=json.dumps(payload).encode("utf-8"),
            )
            status = response.getcode()
            raw = response.read().decode("utf-8")
        except Exception as exc:
            raise SecondLookError(f"Create order request failed: {exc}") from exc

        if not (200 <= status < 300):
            raise SecondLookError(f"Create order error ({status}): {raw}")

        try:
            data = json.loads(raw)
            approve_url = None
            for link in data.get("links", []):
                rel = link.get("rel", "").lower()
                if rel in ("approve", "payer-action"):
                    approve_url = link.get("href")
                    break
            return {
                "id": data.get("id"),
                "status": data.get("status"),
                "approve_url": approve_url,
            }
        except Exception as exc:
            raise SecondLookError(f"Invalid create order response: {raw}") from exc

    def capture_order(self, order_id: str) -> Dict[str, Any]:
        """Capture a previously created PayPal order.

        Args:
            order_id: Identifier of the order to capture.

        Returns:
            Dictionary with capture result containing "id" and "status".

        Raises:
            SecondLookError: On API errors.
        """
        capture_url = f"{self.base_url}/v2/checkout/orders/{order_id}/capture"
        token = self._ensure_token()
        request_id = str(uuid.uuid4())

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "PayPal-Request-Id": request_id,
        }

        # PayPal expects an empty JSON object for capture calls.
        body_bytes = json.dumps({}).encode("utf-8")

        try:
            response = self._http_request(
                "POST", capture_url, headers, data=body_bytes
            )
            status = response.getcode()
            raw = response.read().decode("utf-8")
        except Exception as exc:
            raise SecondLookError(f"Capture order request failed: {exc}") from exc

        if not (200 <= status < 300):
            raise SecondLookError(f"Capture order error ({status}): {raw}")

        try:
            data = json.loads(raw)
            return {"id": data.get("id"), "status": data.get("status")}
        except Exception as exc:
            raise SecondLookError(f"Invalid capture order response: {raw}") from exc
