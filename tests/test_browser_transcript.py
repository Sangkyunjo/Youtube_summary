import unittest
from unittest.mock import patch

from browser_transcript import VisibleTranscriptReader


class Elements:
    def __init__(self, items):
        self._items = items

    @property
    def items(self):
        return self._items() if callable(self._items) else self._items

    def count(self):
        return len(self.items)

    @property
    def first(self):
        return self.items[0]

    def all(self):
        return self.items


class Button:
    def __init__(self, action):
        self.action = action

    def is_visible(self):
        return True

    def click(self, **kwargs):
        self.action()


class Row:
    def __init__(self, text):
        self.text = text

    def is_visible(self):
        return True

    def inner_text(self, **kwargs):
        return self.text

    def scroll_into_view_if_needed(self, **kwargs):
        pass


class Page:
    """Rendered panel fixture, including metadata that hydrates after navigation."""
    def __init__(self, final='2:09'):
        self.url = 'https://www.youtube.com/watch?v=abcdefghijk'
        self.hydrated, self.expanded, self.opened = False, False, False
        self.rows = [Row('0:00\n' + 'A real caption with enough source text. ' * 10),
                     Row(final + '\nThe final spoken sentence.')]

    def get_by_role(self, role, **kwargs):
        return Elements(lambda: [Button(lambda: setattr(self, 'opened', True))] if self.expanded else [])

    def locator(self, selector):
        if selector == 'ytd-watch-metadata':
            return self.Metadata(self)
        return Elements([self.Panel(self)] if self.opened else [])

    def wait_for_timeout(self, ms):
        self.hydrated = True

    class Metadata:
        def __init__(self, page):
            self.page = page

        def get_by_role(self, role, **kwargs):
            return Elements([Button(lambda: setattr(self.page, 'expanded', True))]
                            if self.page.hydrated else [])

    class Panel:
        def __init__(self, page):
            self.page = page

        def is_visible(self):
            return True

        def locator(self, selector):
            return Elements(self.page.rows)


class BrowserTranscriptTest(unittest.TestCase):
    def read(self, page):
        clock = iter(i / 100 for i in range(10000))
        with patch('browser_transcript.time.monotonic', side_effect=lambda: next(clock)):
            return VisibleTranscriptReader(1).read(page, {'video_id': 'abcdefghijk', 'duration_seconds': 139})

    def test_waits_for_rendered_metadata_and_reads_to_final_caption(self):
        page = Page()
        document, status = self.read(page)
        self.assertEqual(status, 'ok')
        self.assertTrue(page.opened)
        self.assertTrue(document['complete'])
        self.assertEqual(document['segments'][-1]['start_seconds'], 129)
        self.assertNotIn('duration', document['segments'][-1])
        self.assertIsNone(document['language'])

    def test_partial_panel_is_not_imported_as_complete(self):
        document, status = self.read(Page(final='0:20'))
        self.assertIsNone(document)
        self.assertEqual(status, 'ui_incomplete_or_loading')

    def test_other_video_or_non_youtube_page_is_rejected(self):
        for url in ['https://www.youtube.com/watch?v=bcdefghijkl',
                    'https://example.com/watch?v=abcdefghijk']:
            page = Page(); page.url = url
            self.assertEqual(self.read(page)[1], 'ui_wrong_video')
