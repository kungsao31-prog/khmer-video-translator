package com.cnkhai.app;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

public class MainActivity extends Activity {
    private static final int PICK_FILE = 4102;
    private static final String APP_URL = "https://cn-kh-ai-api.onrender.com/";
    private WebView webView;
    private ValueCallback<Uri[]> fileCallback;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        webView = new WebView(this);
        setContentView(webView);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        webView.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                if ("https".equalsIgnoreCase(u.getScheme()) &&
                    "cn-kh-ai-api.onrender.com".equalsIgnoreCase(u.getHost())) return false;
                startActivity(new Intent(Intent.ACTION_VIEW, u));
                return true;
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb,
                    FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = cb;
                try { startActivityForResult(params.createIntent(), PICK_FILE); }
                catch (Exception e) {
                    fileCallback = null;
                    Toast.makeText(MainActivity.this, "មិនអាចបើកឯកសារបានទេ", Toast.LENGTH_LONG).show();
                    return false;
                }
                return true;
            }
        });
        webView.setDownloadListener((url, agent, disposition, mime, length) -> {
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                req.setMimeType(mime);
                req.addRequestHeader("User-Agent", agent);
                String cookie = CookieManager.getInstance().getCookie(url);
                if (cookie != null) req.addRequestHeader("Cookie", cookie);
                req.setTitle("CN-KH AI");
                req.setDescription("កំពុងទាញយកឯកសារ...");
                req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS,
                    mime != null && mime.contains("mp4") ? "CN-KH-AI-Khmer.mp4" : "CN-KH-AI-Khmer.mp3");
                ((DownloadManager)getSystemService(DOWNLOAD_SERVICE)).enqueue(req);
                Toast.makeText(this, "បានចាប់ផ្តើមទាញយក", Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(this, "ទាញយកមិនបានសម្រេច", Toast.LENGTH_LONG).show();
            }
        });
        if (state == null) webView.loadUrl(APP_URL); else webView.restoreState(state);
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        if (request == PICK_FILE) {
            if (fileCallback != null) {
                fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
                fileCallback = null;
            }
            return;
        }
        super.onActivityResult(request, result, data);
    }
    @Override protected void onSaveInstanceState(Bundle out) {
        webView.saveState(out);
        super.onSaveInstanceState(out);
    }
    @Override public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack(); else super.onBackPressed();
    }
    @Override protected void onDestroy() {
        if (fileCallback != null) fileCallback.onReceiveValue(null);
        if (webView != null) webView.destroy();
        super.onDestroy();
    }
}