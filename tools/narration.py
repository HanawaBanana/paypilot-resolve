"""Generates the demo narration with edge-tts (free, no API key).

    pip install edge-tts
    python tools/narration.py            # writes n*.mp3 into the current dir

The offsets used when mixing are the scene starts measured during the recording
(see docs/MEDIA.md), so narration stays aligned with what is on screen.
"""

import asyncio

import edge_tts

VOICE = "en-US-AvaNeural"
RATE = "+20%"

LINES = {
    "n0": "PayPilot Resolve. One sentence in, a resolved business exception out.",
    "n1": "The goal is a scope change. A buyer dropped one deliverable, so the job needs a refund and a contractor payment. I show it the goal, and a language model proposes a four step plan: collect, capture, refund, pay.",
    "n2": "Nothing has moved yet. The plan is a dependency graph with a business reason on every step, and each money-out step is waiting for one approval that binds its exact amount, currency, recipient and reason. I approve the plan.",
    "n3": "That is the real PayPal sandbox checkout. The buyer approves the way a real buyer would.",
    "n3a": "Meanwhile the console records a stable request id and a payment fingerprint for every operation, so a retry can never pay the same thing twice.",
    "n3b": "PayPal sends the buyer straight back into the console, which verifies the order with PayPal itself before it captures anything.",
    "n3c": "The refund is bound to the capture id PayPal just returned, never to an identifier the model invented.",
    "n3d": "The policy engine also caps exposure: per transaction, rolling twenty four hours, and velocity.",
    "n4": "The capture is verified, and the refund the plan called for follows it.",
    "n5": "Here is the point of this design. I interrupt the run right after the refund completes.",
    "n6": "Resume. The refund is reused, not repeated, and the contractor payout runs once.",
    "n7": "Every decision, dispatch and verification is in this hash chained audit log.",
    "n8": "Now a payout the policy must refuse. A hundred thousand dollars. Denied, with the reason recorded, and zero platform writes.",
    "n9": "And the money itself, in PayPal's own sandbox account.",
    "n10": "AI proposes. Policy authorizes. PayPal executes. Verification decides what happened.",
}

# Milliseconds from the first frame of the video (scene starts measured in the
# recording run: docs/MEDIA.md).
OFFSETS_MS = {
    "n0": 300, "n1": 8300, "n2": 19000, "n3": 30600, "n3a": 36400, "n3b": 50000,
    "n3c": 58000, "n3d": 68000, "n4": 79200, "n5": 84500, "n6": 88600,
    "n7": 96500, "n8": 102400, "n9": 114300, "n10": 130400,
}


async def main() -> None:
    for key, text in LINES.items():
        await edge_tts.Communicate(text, VOICE, rate=RATE).save(key + ".mp3")
        print("wrote", key + ".mp3")


if __name__ == "__main__":
    asyncio.run(main())
