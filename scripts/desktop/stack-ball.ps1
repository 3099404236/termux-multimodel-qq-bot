# stack-ball.ps1 - a small always-on-top ball that mirrors the phone stack's health.
#
# Green means the bot is answering, red means somebody asked and got nothing,
# grey means the ball itself cannot reach the monitor. It polls the same
# status.json the dashboard serves, so the colour rule lives in one place.
#
#   powershell -ExecutionPolicy Bypass -File stack-ball.ps1
#   (or double-click stack-ball.vbs to start it without a console window)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# One ball only - the Start Menu shortcut is easy to click twice, and a second
# ball would sit exactly on top of the first with no way to tell them apart.
$Script:Mutex = New-Object System.Threading.Mutex($true, "Local\StackBallSingleInstance", [ref]$null)
if (-not $Script:Mutex.WaitOne(0)) { exit 0 }

# ---------------------------------------------------------------- configuration

# The phone's address is not fixed - its DHCP lease moved from .243 to .246 on
# its own, which is what made the ball go grey. So nothing is hardcoded: the
# last address that worked is remembered, and if it stops answering the LAN is
# swept for whatever is serving the monitor port.
$Script:Port = 8799
$Script:AddressFile = Join-Path $env:LOCALAPPDATA "stack-ball-address.txt"
$Script:LastGood = if (Test-Path $Script:AddressFile) {
    (Get-Content $Script:AddressFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
} else { "192.168.3.246" }

$Script:Urls = @()
$Script:TryIndex = 0
$Script:Scanning = $false
$Script:PollMs = 30000
# WebClient otherwise waits 100s. A proxy in TUN mode accepts the TCP handshake
# and then never answers, so an unbounded wait means the ball freezes on a
# colour that is no longer true.
$Script:HttpTimeoutMs = 6000
$Script:Size = 46
$Script:StateFile = Join-Path $env:LOCALAPPDATA "stack-ball-position.txt"

$Script:Colors = @{
    ok      = [System.Drawing.Color]::FromArgb(255, 32, 145, 74)
    warn    = [System.Drawing.Color]::FromArgb(255, 32, 145, 74)  # delivered is delivered
    down    = [System.Drawing.Color]::FromArgb(255, 214, 45, 60)
    running = [System.Drawing.Color]::FromArgb(255, 45, 115, 220)
    stale   = [System.Drawing.Color]::FromArgb(255, 214, 145, 32)
    unknown = [System.Drawing.Color]::FromArgb(255, 120, 128, 138)
}

$Script:Status = "unknown"
$Script:BotTip = "还没连上监控"
$Script:BackupState = "unknown"
$Script:BackupTip = "私有备份：未配置"
$Script:Tip = "$($Script:BotTip)`r`n`r`n$($Script:BackupTip)"
$Script:Busy = $false
$Script:BackupConfigFile = Join-Path $env:LOCALAPPDATA "stack-ball-sources.json"
$Script:BackupConfig = $null

# A widget with no window has nowhere to report from. One line per poll makes
# "why is it grey" answerable without attaching a debugger to it.
$Script:LogFile = Join-Path $env:LOCALAPPDATA "stack-ball.log"

function Write-Log([string] $message) {
    try {
        $stamp = Get-Date -Format "MM-dd HH:mm:ss"
        Add-Content -Path $Script:LogFile -Value "$stamp  $message" -Encoding UTF8
        $lines = @(Get-Content -Path $Script:LogFile -ErrorAction SilentlyContinue)
        if ($lines.Count -gt 200) {
            $lines[-100..-1] | Set-Content -Path $Script:LogFile -Encoding UTF8
        }
    } catch { }
}

Write-Log "启动 - Colors=$(if ($null -eq $Script:Colors) { '空' } else { "$($Script:Colors.Count) 项" }) Size=$Script:Size LastGood=$Script:LastGood"

function Read-BackupConfig {
    if (-not (Test-Path -LiteralPath $Script:BackupConfigFile)) {
        $Script:BackupConfig = $null
        return
    }
    try {
        $Script:BackupConfig = Get-Content -LiteralPath $Script:BackupConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        $Script:BackupConfig = $null
        Write-Log "读取备份监控配置失败: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    }
}

function Read-BackupStatus {
    Read-BackupConfig
    if (-not $Script:BackupConfig) {
        $Script:BackupState = "unknown"
        $Script:BackupTip = "私有备份：未配置"
        return
    }

    $statusFile = [string]$Script:BackupConfig.backup_status_file
    if ([string]::IsNullOrWhiteSpace($statusFile) -or -not (Test-Path -LiteralPath $statusFile)) {
        $Script:BackupState = "unknown"
        $Script:BackupTip = "私有备份：尚无运行记录"
        return
    }

    try {
        $backup = Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $state = [string]$backup.state
        switch ($state) {
            "success" {
                $Script:BackupState = "ok"
                $lastSuccess = [string]$backup.last_success_at
                $intervalHours = [Math]::Max(1, [int]$Script:BackupConfig.interval_hours)
                if (-not [string]::IsNullOrWhiteSpace($lastSuccess)) {
                    try {
                        $ageHours = ([DateTimeOffset]::Now - [DateTimeOffset]::Parse($lastSuccess)).TotalHours
                        if ($ageHours -gt ($intervalHours * 2 + 1)) { $Script:BackupState = "stale" }
                    } catch { }
                }
            }
            "failed"  { $Script:BackupState = "down" }
            "running" { $Script:BackupState = "running" }
            default   { $Script:BackupState = "unknown" }
        }

        $stateLabel = switch ($Script:BackupState) {
            "ok"      { "成功" }
            "stale"   { "成功记录已过期" }
            "down"    { "失败" }
            "running" { "正在执行" }
            default   { "未知" }
        }
        $lines = @("私有备份：$stateLabel")
        if ($backup.outcome -eq "no_changes") { $lines += "· 项目无变化，远端已确认" }
        elseif ($backup.outcome -eq "pushed") { $lines += "· 已推送新快照" }
        if ($backup.last_success_at) {
            try { $lines += "· 上次成功 $([DateTimeOffset]::Parse([string]$backup.last_success_at).ToLocalTime().ToString('MM-dd HH:mm:ss'))" }
            catch { $lines += "· 上次成功 $($backup.last_success_at)" }
        }
        if ($backup.commit) { $lines += "· 快照 $(([string]$backup.commit).Substring(0, [Math]::Min(12, ([string]$backup.commit).Length)))" }
        if ($backup.error) { $lines += "✗ $(([string]$backup.error).Substring(0, [Math]::Min(180, ([string]$backup.error).Length)))" }
        $Script:BackupTip = $lines -join "`r`n"
    } catch {
        $Script:BackupState = "unknown"
        $Script:BackupTip = "私有备份：状态文件无法读取"
        Write-Log "读取备份状态失败: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    }
}

Read-BackupConfig

# ------------------------------------------------- layered window (smooth edges)
# A plain Form+Region gives a jagged circle. UpdateLayeredWindow takes a 32bpp
# bitmap with real per-pixel alpha, so the ball and its glow blend into whatever
# is behind them.
if (-not ("StackBall.Native" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace StackBall {
    // Reaching the phone turned out to need three things WebClient does not do
    // by default: a timeout, no proxy, and a pinned source address.
    public class LanWebClient : System.Net.WebClient {
        private readonly int _timeoutMs;
        public LanWebClient(int timeoutMs) { _timeoutMs = timeoutMs; }

        protected override System.Net.WebRequest GetWebRequest(Uri address) {
            var request = base.GetWebRequest(address);
            var http = request as System.Net.HttpWebRequest;
            if (http != null) {
                // Default is 100s. A TUN proxy that accepts the handshake and
                // then goes quiet would pin the ball on a stale colour.
                http.Timeout = _timeoutMs;
                http.ReadWriteTimeout = _timeoutMs;
                http.Proxy = null;

                System.Net.IPAddress target;
                if (System.Net.IPAddress.TryParse(address.Host, out target)) {
                    System.Net.IPAddress local = LocalAddressFor(target);
                    if (local != null) {
                        // Clearing Proxy is not enough: a proxy in TUN mode grabs
                        // traffic by *route*, so it answered a LAN request with
                        // its own 404. Pinning the source to the NIC that owns
                        // the subnet forces the packets out the real interface.
                        http.ServicePoint.BindIPEndPointDelegate =
                            delegate(System.Net.ServicePoint sp, System.Net.IPEndPoint remote, int retry) {
                                return new System.Net.IPEndPoint(local, 0);
                            };
                    }
                }
            }
            return request;
        }

        public static System.Net.IPAddress LocalAddressFor(System.Net.IPAddress target) {
            if (System.Net.IPAddress.IsLoopback(target)) return null;
            byte[] t = target.GetAddressBytes();
            if (t.Length != 4) return null;

            foreach (var nic in System.Net.NetworkInformation.NetworkInterface.GetAllNetworkInterfaces()) {
                if (nic.OperationalStatus != System.Net.NetworkInformation.OperationalStatus.Up) continue;
                foreach (var info in nic.GetIPProperties().UnicastAddresses) {
                    if (info.Address.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork) continue;
                    if (info.IPv4Mask == null) continue;
                    byte[] a = info.Address.GetAddressBytes();
                    byte[] m = info.IPv4Mask.GetAddressBytes();
                    if (m.Length != 4 || (m[0] | m[1] | m[2] | m[3]) == 0) continue;
                    bool same = true;
                    for (int i = 0; i < 4; i++) {
                        if ((a[i] & m[i]) != (t[i] & m[i])) { same = false; break; }
                    }
                    if (same) return info.Address;
                }
            }
            return null;
        }
    }

    // When the remembered address stops answering, sweep every subnet this PC
    // is on for whatever is serving the monitor port. Probes run in parallel and
    // are source-bound for the same reason as above - without that the proxy
    // fakes a successful handshake on every address and the sweep is useless.
    public static class Finder {
        // 198.18/15 is the benchmark range TUN-mode proxies hand themselves, and
        // 100.64/10 is CGNAT/Tailscale. Sweeping those finds only the proxy.
        static bool IsSyntheticSubnet(byte[] a) {
            if (a[0] == 198 && (a[1] == 18 || a[1] == 19)) return true;
            if (a[0] == 100 && a[1] >= 64 && a[1] <= 127) return true;
            if (a[0] == 169 && a[1] == 254) return true;
            return false;
        }

        // An open port proves nothing: a proxy in TUN mode completes the
        // handshake for every address on its subnet. Only a reply that parses as
        // our own status document counts.
        public static bool IsMonitor(string ip, int port, System.Net.IPAddress local, int timeoutMs) {
            try {
                var url = new Uri(string.Format("http://{0}:{1}/status.json", ip, port));
                var request = (System.Net.HttpWebRequest)System.Net.WebRequest.Create(url);
                request.Timeout = timeoutMs;
                request.ReadWriteTimeout = timeoutMs;
                request.Proxy = null;
                if (local != null) {
                    request.ServicePoint.BindIPEndPointDelegate =
                        delegate(System.Net.ServicePoint sp, System.Net.IPEndPoint remote, int retry) {
                            return new System.Net.IPEndPoint(local, 0);
                        };
                }
                using (var response = (System.Net.HttpWebResponse)request.GetResponse()) {
                    if ((int)response.StatusCode != 200) return false;
                    using (var reader = new System.IO.StreamReader(response.GetResponseStream())) {
                        string body = reader.ReadToEnd();
                        return body.Contains("\"overall\"") && body.Contains("\"links\"");
                    }
                }
            } catch { return false; }
        }

        public static string Find(int port, int timeoutMs) {
            var candidates = new System.Collections.Concurrent.ConcurrentBag<string[]>();
            var locals = new System.Collections.Generic.Dictionary<string, System.Net.IPAddress>();
            // Overlapped connects, not a task per address. Task.Run with a
            // blocking wait needs one pool thread each, and the pool only grows
            // by about two threads a second - 500 probes then never get to run
            // inside the sweep window and the phone is reported missing.
            var sockets = new System.Collections.Generic.List<System.Net.Sockets.Socket>();
            var pending = new System.Threading.CountdownEvent(1);

            foreach (var nic in System.Net.NetworkInformation.NetworkInterface.GetAllNetworkInterfaces()) {
                if (nic.OperationalStatus != System.Net.NetworkInformation.OperationalStatus.Up) continue;
                if (nic.NetworkInterfaceType == System.Net.NetworkInformation.NetworkInterfaceType.Loopback) continue;
                foreach (var info in nic.GetIPProperties().UnicastAddresses) {
                    if (info.Address.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork) continue;
                    if (info.IPv4Mask == null) continue;
                    byte[] m = info.IPv4Mask.GetAddressBytes();
                    // /24 only: sweeping anything larger is not worth the wait.
                    if (!(m[0] == 255 && m[1] == 255 && m[2] == 255)) continue;

                    byte[] a = info.Address.GetAddressBytes();
                    if (IsSyntheticSubnet(a)) continue;

                    var localCopy = info.Address;
                    string prefix = string.Format("{0}.{1}.{2}.", a[0], a[1], a[2]);
                    locals[prefix] = localCopy;
                    for (int host = 1; host <= 254; host++) {
                        if (host == a[3]) continue;
                        string ip = prefix + host;
                        try {
                            var socket = new System.Net.Sockets.Socket(
                                System.Net.Sockets.AddressFamily.InterNetwork,
                                System.Net.Sockets.SocketType.Stream,
                                System.Net.Sockets.ProtocolType.Tcp);
                            socket.Bind(new System.Net.IPEndPoint(localCopy, 0));
                            sockets.Add(socket);
                            pending.AddCount();
                            var captured = socket;
                            string capturedPrefix = prefix;
                            socket.BeginConnect(System.Net.IPAddress.Parse(ip), port, delegate(IAsyncResult ar) {
                                try {
                                    captured.EndConnect(ar);
                                    candidates.Add(new string[] { ip, capturedPrefix });
                                } catch { }
                                finally {
                                    try { captured.Close(); } catch { }
                                    try { pending.Signal(); } catch { }
                                }
                            }, null);
                        } catch { }
                    }
                }
            }

            pending.Signal();
            pending.Wait(timeoutMs + 1500);
            // Closing an unfinished socket completes its callback, so the
            // countdown drains instead of leaving connects dangling.
            foreach (var socket in sockets) { try { socket.Close(); } catch { } }

            foreach (var candidate in candidates) {
                System.Net.IPAddress local;
                locals.TryGetValue(candidate[1], out local);
                if (IsMonitor(candidate[0], port, local, 3000)) return candidate[0];
            }
            return null;
        }
    }

    public static class Native {
        [StructLayout(LayoutKind.Sequential)] public struct Point  { public int X, Y; }
        [StructLayout(LayoutKind.Sequential)] public struct Size   { public int W, H; }
        [StructLayout(LayoutKind.Sequential, Pack = 1)]
        public struct BlendFunction {
            public byte BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat;
        }

        [DllImport("user32.dll", SetLastError = true)]
        static extern int GetWindowLong(IntPtr hWnd, int nIndex);
        [DllImport("user32.dll", SetLastError = true)]
        static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);
        [DllImport("user32.dll", SetLastError = true)]
        static extern bool UpdateLayeredWindow(IntPtr hWnd, IntPtr hdcDst,
            ref Point pptDst, ref Size psize, IntPtr hdcSrc, ref Point pptSrc,
            int crKey, ref BlendFunction pblend, int dwFlags);
        [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr hWnd);
        [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
        [DllImport("gdi32.dll")]  static extern IntPtr CreateCompatibleDC(IntPtr hDC);
        [DllImport("gdi32.dll")]  static extern bool DeleteDC(IntPtr hdc);
        [DllImport("gdi32.dll")]  static extern IntPtr SelectObject(IntPtr hdc, IntPtr h);
        [DllImport("gdi32.dll")]  static extern bool DeleteObject(IntPtr hObject);

        const int GWL_EXSTYLE = -20;
        const int WS_EX_LAYERED = 0x80000;
        const int ULW_ALPHA = 2;

        public static void MakeLayered(Form form) {
            int style = GetWindowLong(form.Handle, GWL_EXSTYLE);
            SetWindowLong(form.Handle, GWL_EXSTYLE, style | WS_EX_LAYERED);
        }

        public static void Paint(Form form, Bitmap bitmap) {
            IntPtr screen = GetDC(IntPtr.Zero);
            IntPtr mem = CreateCompatibleDC(screen);
            IntPtr hBitmap = IntPtr.Zero;
            IntPtr old = IntPtr.Zero;
            try {
                hBitmap = bitmap.GetHbitmap(Color.FromArgb(0));
                old = SelectObject(mem, hBitmap);

                Size size = new Size { W = bitmap.Width, H = bitmap.Height };
                Point source = new Point { X = 0, Y = 0 };
                Point target = new Point { X = form.Left, Y = form.Top };
                BlendFunction blend = new BlendFunction {
                    BlendOp = 0, BlendFlags = 0, SourceConstantAlpha = 255, AlphaFormat = 1
                };
                UpdateLayeredWindow(form.Handle, screen, ref target, ref size,
                    mem, ref source, 0, ref blend, ULW_ALPHA);
            } finally {
                ReleaseDC(IntPtr.Zero, screen);
                if (hBitmap != IntPtr.Zero) { SelectObject(mem, old); DeleteObject(hBitmap); }
                DeleteDC(mem);
            }
        }
    }
}
"@ -ReferencedAssemblies System.Drawing, System.Windows.Forms
}

# ------------------------------------------------------------------- rendering

function New-BallBitmap {
    param(
        [System.Drawing.Color] $Color,
        [System.Drawing.Color] $SecondaryColor
    )

    if (-not $SecondaryColor) { $SecondaryColor = $Color }

    $s = $Script:Size
    $bitmap = New-Object System.Drawing.Bitmap($s, $s, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bitmap)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    # Draw each half independently so one compact ball can carry two sources.
    $pad = 3
    $rect = New-Object System.Drawing.Rectangle($pad, $pad, ($s - 1 - 2 * $pad), ($s - 1 - 2 * $pad))
    $halfWidth = [int][Math]::Ceiling($s / 2.0)
    $halves = @(
        @{ Color = $Color; Clip = (New-Object System.Drawing.Rectangle(0, 0, $halfWidth, $s)) },
        @{ Color = $SecondaryColor; Clip = (New-Object System.Drawing.Rectangle([int]($s / 2), 0, $halfWidth, $s)) }
    )
    foreach ($half in $halves) {
        $saved = $g.Save()
        $g.SetClip($half.Clip)

        $halfColor = [System.Drawing.Color]$half.Color
        $halo = [System.Drawing.Color]::FromArgb(70, $halfColor.R, $halfColor.G, $halfColor.B)
        $haloBrush = New-Object System.Drawing.SolidBrush($halo)
        $g.FillEllipse($haloBrush, 0, 0, $s - 1, $s - 1)
        $haloBrush.Dispose()

        $path = New-Object System.Drawing.Drawing2D.GraphicsPath
        $path.AddEllipse($rect)
        $gradient = New-Object System.Drawing.Drawing2D.PathGradientBrush($path)
        $gradient.CenterPoint = New-Object System.Drawing.PointF(($pad + $rect.Width * 0.35), ($pad + $rect.Height * 0.3))
        $lighter = [System.Drawing.Color]::FromArgb(255,
            [Math]::Min(255, [int]$halfColor.R + 62),
            [Math]::Min(255, [int]$halfColor.G + 62),
            [Math]::Min(255, [int]$halfColor.B + 62))
        $gradient.CenterColor = $lighter
        $gradient.SurroundColors = @($halfColor)
        $g.FillEllipse($gradient, $rect)
        $gradient.Dispose()
        $path.Dispose()
        $g.Restore($saved)
    }

    # Thin rim for definition against a same-coloured background.
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(90, 0, 0, 0), 1)
    $g.DrawEllipse($pen, $rect)
    $pen.Dispose()
    $divider = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(85, 0, 0, 0), 1)
    $g.DrawLine($divider, [int]($s / 2), ($pad + 2), [int]($s / 2), ($s - $pad - 3))
    $divider.Dispose()

    $g.Dispose()
    return $bitmap
}

function Update-Ball {
    if (-not $Script:Form) { Write-Log "重绘跳过：窗口还没建好"; return }

    Read-BackupStatus
    $Script:Step = "取颜色"
    $color = $Script:Colors[$Script:Status]
    if (-not $color) { $color = $Script:Colors.unknown }
    $backupColor = $Script:Colors[$Script:BackupState]
    if (-not $backupColor) { $backupColor = $Script:Colors.unknown }
    $Script:Step = "画位图"
    $bitmap = New-BallBitmap -Color $color -SecondaryColor $backupColor
    $Script:Step = "贴到窗口"
    if (-not $bitmap) { Write-Log "画位图返回了空"; return }
    try { [StackBall.Native]::Paint($Script:Form, $bitmap) } finally { $bitmap.Dispose() }
    $Script:Step = "设置提示"

    $Script:Tip = "$($Script:BotTip)`r`n`r`n$($Script:BackupTip)"
    # The tooltip is optional decoration; losing it must not cost the colour.
    if ($Script:ToolTip) {
        $Script:ToolTip.SetToolTip($Script:Form, $Script:Tip)
    } else {
        Write-Log "提示框对象为空，颜色已更新但悬停无内容"
    }
}

# --------------------------------------------------------------------- polling

function Read-Status {
    if ($Script:Busy) { return }
    $Script:Busy = $true
    # Rebuilt each poll so a re-discovered address takes effect immediately.
    $Script:Urls = @(
        "http://$($Script:LastGood):$($Script:Port)/status.json",
        "http://127.0.0.1:$($Script:Port)/status.json"
    )
    $Script:TryIndex = 0
    Invoke-Attempt
}

function Invoke-Attempt {
    if ($Script:TryIndex -ge $Script:Urls.Count) {
        # Both known routes are dead. The address probably moved again, so sweep
        # the LAN once before giving up - but only one sweep per failure, or a
        # genuinely offline phone would mean scanning every 30 seconds forever.
        if (-not $Script:Scanning) {
            $Script:Scanning = $true
            $Script:BotTip = "正在扫描局域网找手机…"
            Update-Ball
            Write-Log "两条路都不通，开始扫描局域网"
            $found = [StackBall.Finder]::Find($Script:Port, 400)
            Write-Log "扫描结果: $(if ($found) { $found } else { '没找到' })"
            if ($found -and $found -ne $Script:LastGood) {
                $Script:LastGood = $found
                Set-Content -Path $Script:AddressFile -Value $found -Encoding UTF8
                $Script:Urls = @("http://${found}:$($Script:Port)/status.json")
                $Script:TryIndex = 0
                $Script:Scanning = $false
                Invoke-Attempt
                return
            }
        }
        $Script:Scanning = $false
        $Script:Status = "unknown"
        $Script:BotTip = @(
            "连不上监控",
            "",
            "试过：",
            "  · 上次可用地址 $($Script:LastGood):$($Script:Port)",
            "  · adb forward 127.0.0.1:$($Script:Port)",
            "  · 扫描本机所在的 /24 网段",
            "",
            "手机可能不在这个局域网上，或者监控挂了。",
            "（右键可以立即重试）"
        ) -join "`r`n"
        $Script:Busy = $false
        Update-Ball
        return
    }

    $url = $Script:Urls[$Script:TryIndex]
    # The callback fires long after this function returns, so pin the address it
    # needs on the script scope rather than relying on a captured local.
    $Script:CurrentUrl = $url
    $client = New-Object StackBall.LanWebClient($Script:HttpTimeoutMs)
    $client.Encoding = [System.Text.Encoding]::UTF8

    # Async so the ball never freezes while the phone is slow to answer.
    $client.add_DownloadStringCompleted({
        param($sender, $e)

        # Fetching and repainting are kept apart on purpose. They used to share
        # one try/catch, so a null-reference while repainting was read as "this
        # route is down" and the ball moved on to the next URL even though the
        # data had already arrived intact.
        $data = $null
        try {
            if ($e.Error) { throw $e.Error }
            $data = $e.Result | ConvertFrom-Json
        } catch {
            Write-Log "失败 $($Script:CurrentUrl) -> $($_.Exception.Message.Split([Environment]::NewLine)[0])"
            if ($sender) { try { $sender.Dispose() } catch { } }
            $Script:TryIndex = $Script:TryIndex + 1
            Invoke-Attempt
            return
        }

        if ($sender) { try { $sender.Dispose() } catch { } }

        try {

            $Script:Status = $data.overall
            $bad = @($data.links | Where-Object { -not $_.ok })
            $soft = @($data.links | Where-Object { $_.ok -and $_.warn })

            $lines = @("机器人链路：" + $(switch ($data.overall) {
                "ok"   { "一切正常" }
                "warn" { "正常（有个别失败，但都发出去了）" }
                "down" { "有链路故障" }
                default { $data.overall }
            }))
            foreach ($link in $bad)  { $lines += "✗ $($link.name)：$($link.detail)" }
            foreach ($link in $soft) { $lines += "· $($link.name)：$($link.detail)" }
            $source = ([Uri]$Script:CurrentUrl).Host
            $lines += "", "来自 $source　更新于 $($data.generated_local)"
            $lines += "（单击打开面板）"
            $Script:BotTip = ($lines -join "`r`n")

            # Remember whichever address answered, so a lease change costs one
            # sweep rather than a permanently grey ball.
            if ($source -ne "127.0.0.1" -and $source -ne $Script:LastGood) {
                $Script:LastGood = $source
                Set-Content -Path $Script:AddressFile -Value $source -Encoding UTF8
                Write-Log "地址已更新为 $source"
            }
            Write-Log "ok  $source  overall=$($data.overall)"
            $Script:Scanning = $false
        } catch {
            # Never re-route on a rendering problem; the data was good.
            Write-Log "取到数据但收尾出错: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
        }

        $Script:Busy = $false
        try { Update-Ball } catch { Write-Log "重绘失败[$($Script:Step)] 行$($_.InvocationInfo.ScriptLineNumber): 《$($_.InvocationInfo.Line.Trim())》 -> $($_.Exception.Message.Split([Environment]::NewLine)[0])" }
    })

    try {
        $client.DownloadStringAsync([Uri]$url)
    } catch {
        $Script:TryIndex = $Script:TryIndex + 1
        Invoke-Attempt
    }
}

# ------------------------------------------------------------------------ form

$Script:Form = New-Object System.Windows.Forms.Form
$Script:Form.FormBorderStyle = "None"
$Script:Form.ShowInTaskbar = $false
$Script:Form.TopMost = $true
$Script:Form.StartPosition = "Manual"
$Script:Form.Size = New-Object System.Drawing.Size($Script:Size, $Script:Size)
$Script:Form.Text = "机器人状态"

# Restore where it was left, but never off-screen (monitors get unplugged).
$area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$location = New-Object System.Drawing.Point(($area.Right - $Script:Size - 24), ($area.Bottom - $Script:Size - 90))
if (Test-Path $Script:StateFile) {
    $parts = (Get-Content $Script:StateFile -ErrorAction SilentlyContinue) -split ","
    if ($parts.Count -eq 2) {
        $x = [int]$parts[0]; $y = [int]$parts[1]
        $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
        if ($x -ge $bounds.Left -and $y -ge $bounds.Top -and
            $x -le ($bounds.Right - 10) -and $y -le ($bounds.Bottom - 10)) {
            $location = New-Object System.Drawing.Point($x, $y)
        }
    }
}
$Script:Form.Location = $location

$Script:ToolTip = New-Object System.Windows.Forms.ToolTip
$Script:ToolTip.InitialDelay = 250
$Script:ToolTip.ReshowDelay = 100
$Script:ToolTip.AutoPopDelay = 30000

# --------------------------------------------------------------- interactions

$Script:Dragging = $false
$Script:DragFrom = New-Object System.Drawing.Point(0, 0)
$Script:Moved = $false

# Both click paths indexed $Script:Urls with a variable that had been renamed
# out of existence, so they opened $null and failed silently - a widget with
# no console has nowhere to report that. Derive the page from whichever
# address is actually answering instead.
function Get-PanelUrl {
    Read-BackupConfig
    if ($Script:BackupConfig -and -not [string]::IsNullOrWhiteSpace([string]$Script:BackupConfig.backup_panel_url)) {
        return [string]$Script:BackupConfig.backup_panel_url
    }
    $panelHost = $Script:LastGood
    if ($Script:Status -ne "unknown" -and $Script:CurrentUrl) {
        try { $panelHost = ([Uri]$Script:CurrentUrl).Host } catch { }
    }
    return "http://${panelHost}:$($Script:Port)/"
}

function Open-Panel {
    $url = Get-PanelUrl
    try {
        Start-Process $url
        Write-Log "打开面板 $url"
    } catch {
        Write-Log "打开面板失败 $url -> $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    }
}

function Start-ConfiguredBackup {
    Read-BackupConfig
    $taskName = if ($Script:BackupConfig) { [string]$Script:BackupConfig.backup_task_name } else { "" }
    if ([string]::IsNullOrWhiteSpace($taskName)) {
        Write-Log "立即备份跳过：没有配置计划任务"
        return
    }
    try {
        Start-ScheduledTask -TaskName $taskName
        $Script:BackupState = "running"
        $Script:BackupTip = "私有备份：已请求立即运行"
        Update-Ball
        Write-Log "已请求计划任务 $taskName"
    } catch {
        $Script:BackupState = "down"
        $Script:BackupTip = "私有备份：启动失败"
        Update-Ball
        Write-Log "启动备份计划任务失败: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    }
}

$Script:Form.Add_MouseDown({
    if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $Script:Dragging = $true
        $Script:Moved = $false
        $Script:DragFrom = [System.Windows.Forms.Cursor]::Position
        $Script:GrabAt = $Script:Form.Location
    }
})

$Script:Form.Add_MouseMove({
    if (-not $Script:Dragging) { return }
    $now = [System.Windows.Forms.Cursor]::Position
    $dx = $now.X - $Script:DragFrom.X
    $dy = $now.Y - $Script:DragFrom.Y
    if ([Math]::Abs($dx) -gt 2 -or [Math]::Abs($dy) -gt 2) { $Script:Moved = $true }
    $Script:Form.Location = New-Object System.Drawing.Point(($Script:GrabAt.X + $dx), ($Script:GrabAt.Y + $dy))
    Update-Ball   # a layered window must be repainted at its new position
})

$Script:Form.Add_MouseUp({
    if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Right) {
        # A layered window that is never activated gets no WM_CONTEXTMENU,
        # so assigning ContextMenuStrip is not enough - show it by hand.
        if ($Script:Menu) {
            try { $Script:Menu.Show([System.Windows.Forms.Cursor]::Position) }
            catch { Write-Log "弹菜单失败: $($_.Exception.Message.Split([Environment]::NewLine)[0])" }
        }
        return
    }
    if ($_.Button -ne [System.Windows.Forms.MouseButtons]::Left) { return }
    if (-not $Script:Dragging) { return }
    $Script:Dragging = $false

    # WinForms swallows exceptions thrown inside a PowerShell event
    # handler, so anything failing here would silently eat the click.
    try {
        if ($Script:Moved) {
            # Only a real drag is worth persisting; writing the position on
            # every plain click ran ahead of Open-Panel and, when that write
            # hiccuped, the click did nothing at all.
            $where = "$($Script:Form.Location.X),$($Script:Form.Location.Y)"
            Set-Content -Path $Script:StateFile -Value $where -Encoding UTF8
        } else {
            Open-Panel
        }
    } catch {
        Write-Log "松开处理出错: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
    }
})

$Script:Menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$Script:Menu.Items.Add("立即刷新", $null, { Read-Status })
[void]$Script:Menu.Items.Add("打开面板", $null, { Open-Panel })
if ($Script:BackupConfig -and $Script:BackupConfig.backup_task_name) {
    [void]$Script:Menu.Items.Add("立即运行私有备份", $null, { Start-ConfiguredBackup })
}
[void]$Script:Menu.Items.Add("-")
[void]$Script:Menu.Items.Add("退出", $null, { $Script:Form.Close() })
$Script:Form.ContextMenuStrip = $Script:Menu

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $Script:PollMs
$timer.Add_Tick({ Read-Status })

$Script:Form.Add_Shown({
    [StackBall.Native]::MakeLayered($Script:Form)
    Update-Ball
    Read-Status
    $timer.Start()
})

$Script:Form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })

[void]$Script:Form.ShowDialog()
