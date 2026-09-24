from providers import GoogleLive, GoogleSpeech, OpenAIProvider, TypeSafeProvider


def live() -> GoogleLive:
    return GoogleLive()


def transcription() -> OpenAIProvider:
    return OpenAIProvider()


def speech() -> GoogleSpeech:
    return GoogleSpeech()


def decision() -> TypeSafeProvider:
    return TypeSafeProvider()
