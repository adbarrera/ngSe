from numbers import Number
from types import NoneType
from functools import wraps
from time import sleep, time
from atexit import register as register_exit

from urllib2 import URLError
from selenium.webdriver import Chrome, Remote
import selenium.common.exceptions as selenium_exceptions
from selenium.webdriver.common.by import By as selenium_by
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options as ChromeOptions


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

# TODO[TJ]: This can, and should, be sliced off into its own library
def must_be(what, name, types):
    if isinstance(what, types):
        return True
    if isinstance(types, tuple):
        type_list = ", ".join([t.__name__ for t in types])
    else:
        type_list = types.__name__
    raise ValueError(
        "{what} must be of types ({list}), is of type {type}".format(
            what=name,
            list=type_list,
            type=type(what)))


class ByDict(dict):

    """Our own selenium By implementation, allows us to use the text from steps to get By values
    """
    # This is the prefix used to auto-generate NegativeByClause's
    negative_prefix = "NOT_"

    # These two allow us to use attributes and dict keys the same
    def __getattr__(self, item):
        return self[item]

    def __setattr__(self, key, value):
        self[key] = value

    def __getitem__(self, item):
        # Performs generation of NegativeByClause's
        if isinstance(item, basestring):
            if item.startswith(self.negative_prefix):
                return NegativeByClause(self[item[len(self.negative_prefix):]])
        return super(ByDict, self).__getitem__(item)

    def __setitem__(self, key, value):
        # Prevent inaccessible keys from getting in
        if isinstance(key, basestring):
            if key.startswith(self.negative_prefix):
                raise ValueError("Keys in this dict cannot start with '{}'".format(self.negative_prefix))
        super(ByDict, self).__setitem__(key, value)

# Implement the class, add existing values
By = ByDict()
# Is this the best way to do this? Allow retrieving key: None = value: None (for simplifying step parsing)
By[None] = None
for key, value in selenium_by.__dict__.iteritems():
    if not key.startswith('__') and isinstance(value, basestring):
        By[key] = value


class NavigationError(Exception):

    """Happens for things like 404's, 500's, expected element doesn't show up, etc.
    """
    pass


class FrontEndError(Exception):

    """Raised when an error or warning message pops up
    """
    pass


class ByClause(object):

    """Implements a custom by-type, and provides conversion to underlying by-types.
    Instances pass the underlying by-type, and a function that accepts a search value and returns it modified
    appropriately.
    """

    def __init__(self, by, f):
        # Contract
        must_be(by, "by", basestring)
        if not hasattr(f, "__call__"):
            raise ValueError("f must be a callable")
        #
        self.convert = f
        self.by = by

    def __repr__(self):
        # TODO[TJ]: This seems not-good, how can we do a better of this?
        return "<{}: internal by: {}>".format(type(self), self.by)

    def convert(self, *args, **kwargs):
        raise NotImplementedError

    @retry(timeout=5)
    def wait(self, what, browser):
        """Waits for (or tries to) the desired effect, by default this is for the element to be available.
        This is put here to be override-able, so you can, say, wait for the element to 'leave'
        """
        # Contract
        must_be(what, "what", basestring)
        must_be(browser, "browser", Remote)
        #
        return self.find(what, browser)

    def find(self, what, browser):
        """Finds the desired element (what) in the provided browser
        """
        # Contract
        must_be(what, "what", basestring)
        must_be(browser, "browser", Remote)
        #
        what = self.convert(what)
        try:
            return browser.find_element(value=what, by=self.by)
        except selenium_exceptions.NoSuchElementException as e:
            e.msg += "\n  (Element: [{}], By: [{}])".format(what, self.by)
            raise e


class NegativeByClause(ByClause):

    """Takes a ByClause, and returns a subclass who's wait method waits for the element to 'leave'
    """

    def __init__(self, base_by_clause):
        # Contract
        must_be(base_by_clause, "base_by_clause", ByClause)
        #
        ByClause.__init__(self, base_by_clause.by, base_by_clause.convert)

    @retry(timeout=5)
    def wait(self, what, browser):
        """Waits for the desired element to 'leave'. Or tries to.
        """
        # Contract
        must_be(what, "what", basestring)
        must_be(browser, "browser", Remote)
        #
        try:
            self.find(what, browser)
        except cant_see_exceptions:
            return
        else:
            raise ElementStillThereError


def _inner_text_convert(value):
    # Contract
    must_be(value, "value", basestring)
    #
    parts = value.split('\\')
    text = parts.pop()
    others = " and ".join(parts)
    if len(others) > 0:
        others = " and {}".format(others)
    return '//*[contains(text(), "{}"){}]'.format(text, others)


# Implement the class, add existing values
By = ByDict()
# Is this the best way to do this? Allow retrieving key: None = value: None (for simplifying step parsing)
By[None] = None
for key, value in selenium_by.__dict__.iteritems():
    if not key.startswith('__') and isinstance(value, basestring):
        By[key] = ByClause(value, lambda v: v)

By.INNER_TEXT = ByClause(selenium_by.XPATH, _inner_text_convert)
By.NG_CLICK = ByClause(selenium_by.CSS_SELECTOR, lambda v: '[ng-click="{}"]'.format(v))
By.VISIBLE_CLICK = ByClause(selenium_by.CSS_SELECTOR, lambda v: '[ng-click="{}"]:not(.ng-hide)'.format(v))
By.NG_MODEL = ByClause(selenium_by.CSS_SELECTOR, lambda v: '[ng-model="{}"]'.format(v))
By.VISIBLE_MODEL = ByClause(selenium_by.CSS_SELECTOR, lambda v: '[ng-model="{}"]:not(.ng-hide)'.format(v))
By.VISIBLE_SELECTOR = ByClause(selenium_by.CSS_SELECTOR, lambda v: '{}:not(.ng-hide)'.format(v))


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


class NgBrowser(Chrome):
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
        # TODO[TJ]: This should be implimented as part of the future contracts library
        for key, value in pages.iteritems():
            must_be(key, "pages key", basestring)
            must_be(value, "pages value", AppPage)
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
        super(NgBrowser, self).__init__(executable_path=self.executable_path, chrome_options=options)
        register_exit(self.quit)

    def quit(self):
        try:
            super(NgBrowser, self).quit()
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
        value = super(NgBrowser, self).get(url)
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
        notThereExceptions = (
            selenium_exceptions.NoSuchElementException,
            selenium_exceptions.ElementNotVisibleException,
        )

        try:
            self.find_element_by_css_selector('.alertContainer .alert-warning')
        except notThereExceptions:
            pass
        else:
            raise FrontEndError('Warning alert is on screen')

        try:
            self.find_element_by_css_selector('.alertContainer .alert-danger')
        except notThereExceptions:
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
