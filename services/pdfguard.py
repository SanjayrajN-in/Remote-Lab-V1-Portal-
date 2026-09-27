"""What an uploaded lab manual (Experiment.sop_pdf) is allowed to contain.

The VAPT finding: the SOP upload only checked the first bytes for "%PDF-", so
a PDF whose /OpenAction ran JavaScript was accepted and handed to every
student who opened the manual. A header tells you the file is a PDF, not that
it is inert, so the file is parsed here and its object graph inspected.

Manuals are mostly pdflatex/hyperref output (formulae, TOC, \\ref, \\url), and
the rules are shaped so that output passes untouched:

  - /OpenAction as a destination array ([page /Fit]) - hyperref's
    pdfstartview. Only an /OpenAction that is an *action* is checked.
  - /GoTo links (\\ref, \\cite, the TOC), /URI links to http(s) or mailto
    (\\url, \\href), and /Named actions (beamer's navigation bar).
  - Compressed object streams (PDF 1.5+). This is why the check walks the
    parsed objects rather than grepping the bytes: in a pdflatex file most
    dictionaries are inside a Flate stream, and a name like /JavaScript can
    also be written /J#61vaScript. pikepdf decodes both.

Refused: JavaScript anywhere, additional-actions (/AA) triggers, every other
action type (Launch, SubmitForm, ImportData, GoToR, GoToE, RichMediaExecute,
Rendition...), embedded files, XFA forms, rich media, and encrypted files
(which could not be inspected in full). LaTeX packages that produce these on
purpose - animate, media9, attachfile, embedfile, insdljs - are therefore not
supported in uploaded manuals; the error names what was found.
"""
import pikepdf

# Keys whose mere presence means the file can run or carry something.
BLOCKED_KEYS = {
    "/JavaScript": "JavaScript",
    "/JS": "JavaScript",
    "/AA": "automatic actions (/AA)",
    "/EmbeddedFiles": "embedded files",
    "/EF": "embedded files",
    "/XFA": "an XFA form",
    "/RichMedia": "rich media",
    "/RichMediaContent": "rich media",
}
ALLOWED_ACTIONS = {"/GoTo", "/URI", "/Named"}
ALLOWED_URI_SCHEMES = ("http://", "https://", "mailto:")
# Keys whose value is an action (or, for /OpenAction, possibly a destination).
ACTION_KEYS = ("/A", "/OpenAction", "/Next")
# A LaTeX manual has a few thousand objects; this only bounds the walk.
MAX_OBJECTS = 200_000


class UnsafePDF(ValueError):
    """The file is not a PDF we are willing to serve. str() is user-facing."""


def check_pdf(path):
    """Raise UnsafePDF unless `path` is a readable, inert PDF."""
    try:
        with open(path, "rb") as f:
            if not f.read(1024).lstrip(b"\r\n\t \x00").startswith(b"%PDF-"):
                raise UnsafePDF("The manual must be a PDF file.")
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError:
        raise UnsafePDF("The manual is password-protected. Upload an unprotected PDF.")
    except (pikepdf.PdfError, OSError):
        raise UnsafePDF("The manual could not be read as a PDF.")

    with pdf:
        if pdf.is_encrypted:
            raise UnsafePDF("The manual is encrypted. Upload an unprotected PDF.")
        if len(pdf.pages) == 0:
            raise UnsafePDF("The manual has no pages.")
        objects = list(pdf.objects)
        if len(objects) > MAX_OBJECTS:
            raise UnsafePDF("The manual is too complex to check.")
        # pdf.objects is every indirect object, including those packed in
        # object streams; the trailer is the one root that is not among them.
        for obj in [pdf.trailer, *objects]:
            reason = _inspect(obj)
            if reason:
                raise UnsafePDF(f"The manual contains {reason}, which is not "
                                "allowed. Regenerate it without that and upload again.")


def _inspect(obj, depth=0):
    """Return why `obj` is refused, or None. Walks direct children only:
    every indirect object gets its own turn from check_pdf."""
    if depth > 64:
        return "structures nested too deeply"
    if isinstance(obj, pikepdf.Stream):
        obj = obj.stream_dict
    if isinstance(obj, pikepdf.Dictionary):
        if obj.get("/Type") == pikepdf.Name.Action or \
                (obj.get("/Type") == pikepdf.Name.Filespec and "/EF" in obj):
            reason = _check_action(obj) if obj.get("/Type") == pikepdf.Name.Action \
                else "embedded files"
            if reason:
                return reason
        if obj.get("/Type") == pikepdf.Name.EmbeddedFile:
            return "embedded files"
        for key in obj.keys():
            if key in BLOCKED_KEYS:
                return BLOCKED_KEYS[key]
            value = obj.get(key)
            if key in ACTION_KEYS:
                reason = _check_action_value(value)
                if reason:
                    return reason
            if not _is_indirect(value):
                reason = _inspect(value, depth + 1)
                if reason:
                    return reason
    elif isinstance(obj, pikepdf.Array):
        for item in obj:
            if not _is_indirect(item):
                reason = _inspect(item, depth + 1)
                if reason:
                    return reason
    return None


def _check_action_value(value):
    # /OpenAction [3 0 R /Fit] is a destination, not an action: allowed.
    # /Next may be a single action or an array of them.
    if isinstance(value, pikepdf.Array):
        for item in value:
            if isinstance(item, pikepdf.Dictionary):
                reason = _check_action(item)
                if reason:
                    return reason
        return None
    if isinstance(value, pikepdf.Dictionary):
        return _check_action(value)
    return None


def _check_action(action):
    kind = action.get("/S")
    if kind is None:
        return None
    if str(kind) not in ALLOWED_ACTIONS:
        return f"a {str(kind).lstrip('/')} action"
    if str(kind) == "/URI":
        uri = str(action.get("/URI", "")).strip().lower()
        if not uri.startswith(ALLOWED_URI_SCHEMES):
            return "a link that is not http, https or mailto"
    return _check_action_value(action.get("/Next"))


def _is_indirect(obj):
    return isinstance(obj, pikepdf.Object) and obj.is_indirect
