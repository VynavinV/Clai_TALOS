import os
import logging
import aiohttp
from typing import Optional, Dict, Any

logger = logging.getLogger("talos.bland")

# Bland.ai API base URL
BLAND_API_BASE = "https://api.bland.ai/v1"


def is_enabled() -> bool:
    """Check if Bland.ai calling is enabled."""
    return os.getenv("BLAND_ENABLED", "false").lower() == "true"


def get_api_key() -> Optional[str]:
    """Get Bland.ai API key from environment."""
    return os.getenv("BLAND_API_KEY")


async def initiate_call(
    phone_number: str,
    agent_id: Optional[str] = None,
    from_number: Optional[str] = None,
    custom_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Initiate an outbound call via Bland.ai.

    Args:
        phone_number: The phone number to call (E.164 format recommended)
        agent_id: Optional Bland.ai agent/assistant ID to use
        from_number: Optional caller ID number to display
        custom_data: Optional custom data to pass to the agent

    Returns:
        Dict with call_id and status, or error dict
    """
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "BLAND_API_KEY not configured"}

    if not is_enabled():
        return {"ok": False, "error": "Bland.ai calling is not enabled"}

    payload = {
        "phone_number": phone_number,
    }

    if agent_id:
        payload["agent_id"] = agent_id
    if from_number:
        payload["from"] = from_number
    if custom_data:
        payload["custom_data"] = custom_data

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BLAND_API_BASE}/calls",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200 or resp.status == 201:
                    data = await resp.json()
                    logger.info(f"Call initiated to {phone_number}: {data.get('call_id', 'unknown')}")
                    return {"ok": True, "call_id": data.get("call_id"), "data": data}
                else:
                    error_text = await resp.text()
                    logger.error(f"Bland.ai API error {resp.status}: {error_text}")
                    return {"ok": False, "error": f"API error {resp.status}: {error_text}"}
    except Exception as e:
        logger.error(f"Error initiating call: {e}")
        return {"ok": False, "error": str(e)}


async def get_call_status(call_id: str) -> Dict[str, Any]:
    """
    Get status of a call by call_id.

    Args:
        call_id: The call ID from initiate_call

    Returns:
        Dict with call status information
    """
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "BLAND_API_KEY not configured"}

    headers = {
        "Authorization": f"Bearer {api_key}"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BLAND_API_BASE}/calls/{call_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {"ok": True, "data": data}
                else:
                    error_text = await resp.text()
                    logger.error(f"Bland.ai API error {resp.status}: {error_text}")
                    return {"ok": False, "error": f"API error {resp.status}: {error_text}"}
    except Exception as e:
        logger.error(f"Error getting call status: {e}")
        return {"ok": False, "error": str(e)}


async def end_call(call_id: str) -> Dict[str, Any]:
    """
    End an active call.

    Args:
        call_id: The call ID to end

    Returns:
        Dict with success/failure status
    """
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "BLAND_API_KEY not configured"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BLAND_API_BASE}/calls/{call_id}/end",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Call {call_id} ended")
                    return {"ok": True, "data": data}
                else:
                    error_text = await resp.text()
                    logger.error(f"Bland.ai API error {resp.status}: {error_text}")
                    return {"ok": False, "error": f"API error {resp.status}: {error_text}"}
    except Exception as e:
        logger.error(f"Error ending call: {e}")
        return {"ok": False, "error": str(e)}
