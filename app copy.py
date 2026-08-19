import os
from google import genai
from google.genai import types

# Initialize client (uses GEMINI_API_KEY from environment by default)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

video_url = "https://www.youtube.com/watch?v=_m8bqUD3jy4"

response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents=[
        types.Part.from_uri(
            file_uri=video_url,
            mime_type="video/*",
        ),
        "write the exact timestamps of the video where the speaker is saying 'que se levanta temprano'",
    ],
    config=types.GenerateContentConfig(
        temperature=0.2,
    ),
)

print(response.text)
