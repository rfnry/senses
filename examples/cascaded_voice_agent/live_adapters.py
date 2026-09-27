from providers import GoogleLive, GoogleSpeech, OpenAIProvider


def live() -> GoogleLive:
    return GoogleLive()


def transcription() -> OpenAIProvider:
    return OpenAIProvider()


def speech() -> GoogleSpeech:
    return GoogleSpeech()
