"""Safe errors distinguishing rejected tasks from uncertain external submissions."""


class VideoProviderError(RuntimeError):
    """Provider failure without raw response, request, credential or signed URL."""


class SubmissionUnknown(RuntimeError):
    """Submission may have created a remote task; automatic replay is unsafe."""


VideoSubmissionUnknown = SubmissionUnknown
