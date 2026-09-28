import unittest
from unittest.mock import Mock, patch
import qoder_wechat as wx

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, 'Run with browser venv for DOM verification')
class BrowserDOMTests(unittest.TestCase):
    def setUp(self):
        self.runtime = sync_playwright().start()
        self.addCleanup(self.runtime.stop)
        self.browser = self.runtime.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()

    def test_reads_input_credentials_not_just_body_text(self):
        self.page.set_content('<input id="appid" value="wx12345678"><input id="appsecret" value="0123456789abcdef0123456789abcdef">')
        result = self.page.evaluate(wx.EXTRACT_CREDS_JS)
        self.assertEqual(result['appid'], 'wx12345678')
        self.assertEqual(result['appsecret'], '0123456789abcdef0123456789abcdef')

    def test_create_form_does_not_modify_background_controls(self):
        self.page.set_content('''<input id="background" value="keep">
          <button onclick="document.getElementById('dialog').style.display='block'">新增测试模板</button>
          <section id="dialog" style="display:none">
            <h2>新增测试模板</h2>
            <label>模板标题<input id="title"></label>
            <label>模板内容<textarea id="content"></textarea></label>
            <button onclick="this.setAttribute('submitted','yes')">提交</button>
          </section>''')
        self.assertTrue(wx._click_new_template(self.page))
        self.assertTrue(wx._fill_and_submit_template(self.page))
        self.assertEqual(self.page.locator('#title').input_value(), 'Qoder 签到通知')
        self.assertEqual(self.page.locator('#content').input_value(), wx.notify.TEMPLATE)
        self.assertEqual(self.page.locator('#background').input_value(), 'keep')
        self.assertEqual(self.page.locator('[submitted="yes"]').count(), 1)


class BrowserBindingTests(unittest.TestCase):
    def test_existing_compatible_template_never_creates_another(self):
        with patch.object(wx.notify, 'binding_options', return_value=([], ['old','other'])), \
             patch.object(wx, '_click_new_template') as create:
            self.assertEqual(wx.ensure_template(Mock(), {'wx_test_template_id':'old'}), 'old')
            create.assert_not_called()

    def test_missing_template_created_once_and_verified_by_api(self):
        with patch.object(wx.notify, 'binding_options', side_effect=[([],[]),([],[]),([],['created'])]), \
             patch.object(wx, '_click_new_template', return_value=True) as create, \
             patch.object(wx, '_fill_and_submit_template', return_value=True) as submit:
            self.assertEqual(wx.ensure_template(Mock(), {}), 'created')
            create.assert_called_once()
            submit.assert_called_once()

    def test_api_failure_does_not_create_template(self):
        with patch.object(wx.notify, 'binding_options', side_effect=wx.notify.NotifyError('failed')), \
             patch.object(wx, '_click_new_template') as create:
            with self.assertRaises(wx.notify.NotifyError):
                wx.ensure_template(Mock(), {})
            create.assert_not_called()

    def test_unconfirmed_creation_fails_instead_of_repeated_submit(self):
        with patch.object(wx.notify, 'binding_options', return_value=([],[])), \
             patch.object(wx, '_click_new_template', return_value=True), \
             patch.object(wx, '_fill_and_submit_template', return_value=False) as submit:
            with self.assertRaises(wx.notify.NotifyError):
                wx.ensure_template(Mock(), {})
            submit.assert_called_once()

    def test_recipient_ambiguity_is_not_guessed(self):
        self.assertEqual(wx.select_recipient(['a','b'], ['a','b']), '')
        self.assertEqual(wx.select_recipient(['a','b'], ['a','b','new']), 'new')
        self.assertEqual(wx.select_recipient(['a','b'], ['a','b'], 'b'), 'b')
        self.assertEqual(wx.select_recipient([], ['a','b']), '')

    def test_credential_extraction_requires_wechat_https_origin(self):
        self.assertTrue(wx.trusted(Mock(url=wx.URL)))
        self.assertFalse(wx.trusted(Mock(url='https://mp.weixin.qq.com.evil.example/')))


class LoginRecoveryTests(unittest.TestCase):
    def make_context(self):
        context = Mock()
        page = Mock(url=wx.URL)
        context.new_page.return_value = page
        context.pages = [page]
        page.is_closed.return_value = False
        page.frames = [page]
        page.evaluate.return_value = {}
        controls = {}
        def get_text(text, exact=True):
            if text not in controls:
                control = Mock()
                control.first = control
                control.count.return_value = 1 if text in ('初始化失败', '重新登录') else 0
                control.is_visible.return_value = True
                controls[text] = control
            return controls[text]
        page.get_by_text.side_effect = get_text
        return context, page, controls

    def test_initialization_failure_retries_relogin_then_reads_credentials(self):
        context, page, controls = self.make_context()
        def click(**kwargs):
            page.evaluate.return_value = {'appid': 'wx12345678', 'appsecret': 'fake-secret'}
        page.get_by_text('重新登录', exact=True).click.side_effect = click
        with patch.object(wx.time, 'monotonic', side_effect=[0, 1, 2, 301]):
            result = wx.login(context)
        self.assertEqual(result, (page, 'wx12345678', 'fake-secret'))
        controls['重新登录'].click.assert_called_once()

    def test_persistent_initialization_failure_explains_problem_without_retry_loop(self):
        context, page, controls = self.make_context()
        with patch.object(wx.time, 'monotonic', side_effect=[0, 1, 2, 301]):
            with self.assertRaisesRegex(wx.notify.NotifyError, '初始化失败'):
                wx.login(context)
        controls['重新登录'].click.assert_called_once()
