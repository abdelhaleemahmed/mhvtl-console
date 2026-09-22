"""What a URL that only answers POST should say when it is asked for a page.

Some URLs in this console are the action of a form, not a page: adopting a
tape, the library's control form, the iSCSI endpoints the pages call. Django's
View answers a GET on one of those with 405 and an empty body, which in a
browser is a blank white page - and a blank page reads as a broken console
rather than as a URL that was never a page.

Two shapes, because there are two kinds of caller:

``RedirectOnGet``
    for a form's action. Somebody who bookmarked it, or pressed Enter in the
    address bar after submitting, is sent to the page the form lives on.

``JsonOnGet``
    for an endpoint the page calls with JavaScript. It keeps the 405 - it is
    an API and not a page - but says so in the JSON the caller is already
    parsing.
"""
from django.http import JsonResponse
from django.shortcuts import redirect


class RedirectOnGet:
    """Send a GET to the page this form belongs to.

    Set ``page`` to a URL name, and ``page_arguments`` when it takes any::

        class AdoptTapeView(RedirectOnGet, View):
            page = 'libraries:tape_list'
    """

    #: URL name of the page whose form posts here.
    page = None

    def page_arguments(self, request, *args, **kwargs):
        """The arguments for that URL name; the same ones by default."""
        return args, kwargs

    def get(self, request, *args, **kwargs):
        if not self.page:                       # nothing configured; keep 405
            return self.http_method_not_allowed(request, *args, **kwargs)
        positional, named = self.page_arguments(request, *args, **kwargs)
        return redirect(self.page, *positional, **named)


class JsonOnGet:
    """Answer a GET with JSON saying POST is what this endpoint takes."""

    def http_method_not_allowed(self, request, *args, **kwargs):
        allowed = [method.upper() for method in self.http_method_names
                   if hasattr(self, method)]
        return JsonResponse(
            {'success': False,
             'error': f'{request.method} is not allowed here; '
                      f'this endpoint takes {" or ".join(allowed)}'},
            status=405)
