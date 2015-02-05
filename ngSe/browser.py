from numbers import Number
from types import NoneType
from functools import wraps
from time import sleep, time
from atexit import register as register_exit

from urllib2 import URLError
from selenium.webdriver import Chrome
import selenium.common.exceptions as selenium_exceptions
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options as ChromeOptions

from .contract import must_be
from .by import By, ByClause

download_directory = "./tmp"


class ElementStillThereError(Exception):

    """Raised when an element that shouldn't be present, is
    """
    pass


class WaitFailedError(Exception):

    """Raised when a ByClauses wait method fails in a way we *don't* want @retry to catch
    """

    def __init__(self, message, cause):
        super(WaitFailedError, self).__init__("{}, caused by {}".format(message, repr(cause)))
        self.cause = cause


class DontRetryError(Exception):

    """Raised when a retryable error happens, but we don't want to retry
    """

    def __init__(self, message, cause):
        super(DontRetryError, self).__init__("{}, caused by {}".format(message, repr(cause)))
        self.cause = cause


element_exceptions = (
    selenium_exceptions.InvalidElementStateException,
    selenium_exceptions.NoSuchElementException,
    selenium_exceptions.ElementNotVisibleException,
    ElementStillThereError,
    ValueError,
)

cant_see_exceptions = (
    selenium_exceptions.NoSuchElementException,
    selenium_exceptions.ElementNotVisibleException,
    selenium_exceptions.NoSuchElementException,
    ValueError,
)


def retry(f=None, timeout=30, interval=0.1):
    """
    When working with a responsive UI, sometimes elements are not ready at the very second you request it
    This wrapper will keep on retrying finding or interacting with the element until its ready
    """

    # This allows us to use '@retry' or '@retry(timeout=thing, interval=other_thing)' for custom times
    if f is None:
        def rwrapper(f):
            return retry(f, timeout, interval)
        return rwrapper

    @wraps(f)
    def wrapper(*args, **kwargs):
        # The wrapped function gets the optional arguments retry_timeout and retry_interval added
        retry_timeout = kwargs.pop('retry_timeout', timeout)
        retry_interval = kwargs.pop('retry_interval', interval)
        prep = kwargs.pop('prep', None)

        end_time = time() + retry_timeout

        while True:
            try:
                if prep is not None:
                    prep()
                return f(*args, **kwargs)
            except element_exceptions:
                if time() > end_time:
                    # timeout, re-raise the original exception
                    raise
                sleep(retry_interval)

    return wrapper


class NavigationError(Exception):

    """Happens for things like 404's, 500's, expected element doesn't show up, etc.
    """
    pass


class FrontEndError(Exception):

    """Raised when an error or warning message pops up
    """
    pass


class AppPage(object):

    """Object to represent pages to navigate to in the app
    """

    def __init__(self, page, wait_for=None, wait_for_by=By.ID):
        # Contract
        if not isinstance(page, basestring) and not hasattr(page, "__call__"):
            raise ValueError("page must be a string or callable")
        must_be(wait_for, "wait_for", (NoneType, basestring))
        must_be(wait_for_by, "wait_for_by", (ByClause))
        #
        self._page = page
        self.wait_for = wait_for
        self.wait_for_by = wait_for_by

    @property
    def page(self):
        if not isinstance(self._page, basestring) and hasattr(self._page, "__call__"):
            return self._page()
        return self._page


class Browser(Chrome):
    executable_path = '/usr/local/bin/chromedriver'
    app_host = 'localhost'
    app_port = 5000

    def __init__(self, scenario, download_directory=None, app_host=None, app_port=None, executable_path=None, pages={}):
        # Contract
        must_be(download_directory, "download_directory", (NoneType, basestring))
        must_be(app_host, "app_host", (NoneType, basestring))
        must_be(app_port, "app_port", (NoneType, Number))
        must_be(executable_path, "executable_path", (NoneType, basestring))
        must_be(pages, "pages", dict)
        for key, value in pages.iteritems():
            must_be(key, "pages key", basestring)
            must_be(value, "pages value", AppPage)
        # TODO[TJ]: This should be implemented as part of the future contracts library
        #
        self.scenario = scenario
        if download_directory is not None:
            options = ChromeOptions()
            prefs = {"download.default_directory": download_directory}
            options.add_experimental_option('prefs', prefs)
        else:
            options = None
        if app_host is not None:
            self.app_host = app_host
        if app_port is not None:
            self.app_port = app_port
        if executable_path is not None:
            self.executable_path = executable_path
        self.pages = pages
        super(Browser, self).__init__(executable_path=self.executable_path, chrome_options=options)
        register_exit(self.quit)

    def quit(self):
        try:
            super(Browser, self).quit()
        except URLError as e:
            if e.reason.errno == 61 or e.reason.errno == 111:
                # 'Connection refused', this happens when the driver has already closed/quit
                pass
            else:
                raise

    def wait_for(self, value, by=By.ID):
        """Waits for an element to appear on screen
        """
        # Contract
        must_be(value, "value", basestring)
        must_be(by, "by", ByClause)
        #
        by.wait(value, self)

    def goto(self, url):
        """Wrapper to check for navigation issues, like 404's
        """
        # Contract
        must_be(url, "url", basestring)
        #
        value = super(Browser, self).get(url)
        page_title = self.title
        if page_title in {'404 Not Found'}:
            raise NavigationError(page_title)
        return value

    def navigate(self, to):
        """Goes to a page in the app
        """
        # Contract
        must_be(to, "to", (AppPage, basestring))
        #
        if isinstance(to, basestring):
            to = self.pages[to.lower()]
        url = "http://{host}:{port}/{page}".format(host=self.app_host, port=self.app_port, page=to.page)
        return_value = self.goto(url)
        if to.wait_for is not None:
            try:
                retry(to.wait_for_by.wait)(to.wait_for, self)
            except selenium_exceptions.NoSuchElementException:
                raise NavigationError(
                    "Expected element {}:{} didn't show when navigating to {}".format(
                        to.wait_for,
                        to.wait_for_by,
                        to.page))
        return return_value

    @retry(timeout=15)
    def click(self, what, by=By.LINK_TEXT, hover_time=0.1, wait_for=None, wait_for_by=By.ID):
        """Find, hover on, and click on the given element
        """
        # Contract
        must_be(what, "element", basestring)
        must_be(by, "by", ByClause)
        must_be(hover_time, "hover_time", Number)
        must_be(wait_for, "wait_for", (NoneType, basestring))
        must_be(wait_for_by, "wait_for_by", (NoneType, ByClause))
        #
        element = by.find(what, self)
        self.hover_on(element, hover_time)
        return_value = element.click()
        if wait_for is not None:
            # If this fails, we need the whole function to fail (don't want to re-do a successful click)
            try:
                wait_for_by.wait(wait_for, self)
            except cant_see_exceptions as e:
                # TODO[TJ]: This custom exception feels clunky, only used for, and only outside of, the wait method
                raise WaitFailedError("Wait failed", e)
            except element_exceptions as e:
                # Some weird stuff, this shouldn't happen
                raise DontRetryError("Wait failed", e)

        return return_value

    def _scroll_to(self, element, wait_after=0.25):
        """Scroll to view an element
        """
        # Contract
        must_be(element, "element", WebElement)
        must_be(wait_after, "wait_after", Number)
        #
        """Currently a bug in the move_to_element on ActionChains, so we can't use it, must use JS instead.
        This scrolls the element into the middle of the page, useful since we have the top and bottom fixed divs that
        will cover anything scrolled 'just to' the top or bottom.
        """
        self.execute_script(
            "Element.prototype.documentOffsetTop = function () {return this.offsetTop + ( this.offsetParent ? this.offsetParent.documentOffsetTop() : 0 );};")  # NOQA
        self.execute_script(
            "window.scrollTo( 0, arguments[0].documentOffsetTop()-(window.innerHeight / 2 ));", element)
        sleep(wait_after)

    def hover_on(self, element, hover_time=0.1):
        """Hover the mouse on an element
        """
        # Contract
        must_be(element, "element", WebElement)
        must_be(hover_time, "hover_time", Number)
        #
        self._scroll_to(element)
        chain = ActionChains(self).move_to_element(element)
        sleep(hover_time)
        return chain.perform()

    def _fill(self, element, text, by=By.ID, check=True, check_against=None, check_attribute="value", empty=False):
        """Fills in a given element with the given text, optionally checking emptying it first and/or checking the
        contents after (optionally against a different value).
        """
        # Contract
        must_be(element, "element", WebElement)
        must_be(text, "text", basestring)
        must_be(by, "by", ByClause)
        must_be(check, "check", bool)
        must_be(check_against, "check_against", (NoneType, basestring))
        must_be(check_attribute, "check_attribute", basestring)
        must_be(empty, "empty", bool)
        #
        if empty:
            element.clear()
        return_value = element.send_keys(text)
        if check:
            if check_against is None:
                check_against = text
            assert check_against in element.get_attribute(check_attribute)
        return return_value

    def fill(self, what, text, by=By.ID, check=True, check_against=None, check_attribute="value", empty=False):
        """Finds and fills in an element with the given text.
        """
        # Contract
        must_be(what, "element", basestring)
        must_be(text, "text", basestring)
        must_be(by, "by", ByClause)
        must_be(check, "check", bool)
        must_be(check_against, "check_against", (NoneType, basestring))
        must_be(check_attribute, "check_attribute", basestring)
        must_be(empty, "empty", bool)
        #
        element = self.find_element(value=what, by=by)
        return self._fill(element, text, by, check, check_against, check_attribute, empty)

    @retry
    def wait_for_success(self):
        not_there_exceptions = (
            selenium_exceptions.NoSuchElementException,
            selenium_exceptions.ElementNotVisibleException,
        )

        try:
            self.find_element_by_css_selector('.alertContainer .alert-warning')
        except not_there_exceptions:
            pass
        else:
            raise FrontEndError('Warning alert is on screen')

        try:
            self.find_element_by_css_selector('.alertContainer .alert-danger')
        except not_there_exceptions:
            pass
        else:
            raise FrontEndError('Danger alert is on screen')

        self.find_element_by_css_selector('.alertContainer .alert-success')
        # Close the alert
        alert = self.find_element_by_css_selector('.alert button.close')
        alert.click()

    def text_is_present(self, text, *args, **kwargs):
        try:
            self._text_is_present(text, *args, **kwargs)
        except cant_see_exceptions:
            return False
        else:
            return True

    @retry
    def _text_is_present(self, text):
        self.find_element_by_tag_name('body').text.index(text)

    @retry
    def wait_for_element(self, value, by=By.CSS_SELECTOR):
        must_be(by, "by", ByClause)
        return by.wait(value, self)