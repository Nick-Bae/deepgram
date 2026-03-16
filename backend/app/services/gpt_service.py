import os
from dotenv import load_dotenv
load_dotenv()  # This loads the .env file so os.getenv() works

import openai
from openai import OpenAI
from app.env import ENV

client = OpenAI(api_key=ENV.OPENAI_API_KEY)
# openai.api_key = os.getenv("OPENAI_API_KEY")

def translate_text(text: str, source: str, target: str) -> str:
    system_prompt = (
        f"You are a theological translator. "
        f"Translate from {source} to {target}, keeping spiritual and theological accuracy. "
        f"Do not summarize. Output only the translated text."
    )

    response = client.chat.completions.create(
        model=ENV.resolve_translation_model(),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    )

    translated = response.choices[0].message.content.strip()
    return translated
