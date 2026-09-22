"""Values every page can show without each view fetching them."""
from mhvtl_system import __version__


def version(request):
    """The console's version, for the footer and the about page."""
    return {'gui_version': __version__}
