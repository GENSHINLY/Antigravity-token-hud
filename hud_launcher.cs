using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;

internal static class Program
{
    private const string MUTEX_NAME = @"Global\AntigravityTokenHUDMutex";
    private const string LOCAL_MUTEX_NAME = @"Local\AntigravityTokenHUDMutex";
    private const string WINDOW_TITLE = "Antigravity & Argon Token HUD";

    private const uint SYNCHRONIZE = 0x00100000;
    private const int SW_SHOW = 5;
    private const int SW_RESTORE = 9;
    private const uint SWP_NOSIZE = 0x0001;
    private const uint SWP_NOMOVE = 0x0002;
    private const uint SWP_SHOWWINDOW = 0x0040;
    private static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenMutexW(uint dwDesiredAccess, bool bInheritHandle, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr FindWindowW(string lpClassName, string lpWindowName);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(
        IntPtr hWnd,
        IntPtr hWndInsertAfter,
        int X,
        int Y,
        int cx,
        int cy,
        uint uFlags);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    private static bool IsHudAlreadyRunning()
    {
        IntPtr hMutex = OpenMutexW(SYNCHRONIZE, false, MUTEX_NAME);
        if (hMutex != IntPtr.Zero)
        {
            CloseHandle(hMutex);
            return true;
        }
        hMutex = OpenMutexW(SYNCHRONIZE, false, LOCAL_MUTEX_NAME);
        if (hMutex != IntPtr.Zero)
        {
            CloseHandle(hMutex);
            return true;
        }
        return false;
    }

    private static bool BringExistingHudToFront()
    {
        IntPtr hwnd = FindWindowW(null, WINDOW_TITLE);
        if (hwnd == IntPtr.Zero)
        {
            return false;
        }

        if (IsIconic(hwnd))
        {
            ShowWindow(hwnd, SW_RESTORE);
        }
        else
        {
            ShowWindow(hwnd, SW_SHOW);
        }

        SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW);
        BringWindowToTop(hwnd);
        SetForegroundWindow(hwnd);
        return true;
    }

    private static string QuoteArg(string arg)
    {
        string q = new string('"', 1);
        string bs = new string((char)92, 1);
        if (string.IsNullOrEmpty(arg))
        {
            return q + q;
        }
        if (arg.IndexOfAny(new char[] { ' ', (char)9, '"' }) < 0)
        {
            return arg;
        }
        return q + arg.Replace(q, bs + q) + q;
    }

    private static string GetCompileTimeSourceDir([CallerFilePath] string sourceFilePath = "")
    {
        if (string.IsNullOrEmpty(sourceFilePath))
        {
            return string.Empty;
        }
        try
        {
            return Path.GetDirectoryName(sourceFilePath) ?? string.Empty;
        }
        catch
        {
            return string.Empty;
        }
    }

    private static string NormalizeCandidatePath(string rawPath, string baseDir)
    {
        if (string.IsNullOrWhiteSpace(rawPath))
        {
            return string.Empty;
        }
        string cleaned = Environment.ExpandEnvironmentVariables(rawPath.Trim().Trim('"'));
        if (cleaned.Length >= 2 && cleaned[0] == '~' && (cleaned[1] == '/' || cleaned[1] == Path.DirectorySeparatorChar))
        {
            string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            cleaned = Path.Combine(home, cleaned.Substring(2));
        }
        try
        {
            if (!Path.IsPathRooted(cleaned) && !string.IsNullOrEmpty(baseDir))
            {
                cleaned = Path.GetFullPath(Path.Combine(baseDir, cleaned));
            }
            if (Directory.Exists(cleaned))
            {
                string candidateInDir = Path.Combine(cleaned, "token_hud.py");
                if (File.Exists(candidateInDir))
                {
                    return candidateInDir;
                }
            }
            if (File.Exists(cleaned))
            {
                return cleaned;
            }
        }
        catch
        {
        }
        return string.Empty;
    }

    private static string UnescapeJsonString(string raw)
    {
        if (string.IsNullOrEmpty(raw))
        {
            return string.Empty;
        }
        char bs = (char)92;
        StringBuilder sb = new StringBuilder(raw.Length);
        for (int i = 0; i < raw.Length; i++)
        {
            char c = raw[i];
            if (c == bs && i + 1 < raw.Length)
            {
                char next = raw[++i];
                if (next == 'n') sb.Append('\n');
                else if (next == 'r') sb.Append('\r');
                else if (next == 't') sb.Append((char)9);
                else sb.Append(next);
            }
            else
            {
                sb.Append(c);
            }
        }
        return sb.ToString();
    }

    private static string TryExtractJsonValue(string json, string key)
    {
        string token = "\"" + key + "\"";
        int idx = 0;
        char bs = (char)92;
        while ((idx = json.IndexOf(token, idx, StringComparison.Ordinal)) >= 0)
        {
            int afterKey = idx + token.Length;
            while (afterKey < json.Length && char.IsWhiteSpace(json[afterKey])) afterKey++;
            if (afterKey < json.Length && json[afterKey] == ':')
            {
                int valStart = afterKey + 1;
                while (valStart < json.Length && char.IsWhiteSpace(json[valStart])) valStart++;
                if (valStart < json.Length && json[valStart] == '"')
                {
                    valStart++;
                    int valEnd = valStart;
                    while (valEnd < json.Length)
                    {
                        if (json[valEnd] == bs)
                        {
                            valEnd += 2;
                            continue;
                        }
                        if (json[valEnd] == '"')
                        {
                            return UnescapeJsonString(json.Substring(valStart, valEnd - valStart));
                        }
                        valEnd++;
                    }
                }
            }
            idx = afterKey;
        }
        return string.Empty;
    }

    private static string TryReadPathFromJsonConfig(string jsonFilePath)
    {
        if (!File.Exists(jsonFilePath))
        {
            return string.Empty;
        }
        try
        {
            string content = File.ReadAllText(jsonFilePath, Encoding.UTF8);
            string baseDir = Path.GetDirectoryName(jsonFilePath) ?? string.Empty;
            string[] keys = new string[] { "script_path", "scriptPath", "token_hud_path", "hud_path", "path" };
            foreach (string key in keys)
            {
                string rawVal = TryExtractJsonValue(content, key);
                if (!string.IsNullOrEmpty(rawVal))
                {
                    string resolved = NormalizeCandidatePath(rawVal, baseDir);
                    if (!string.IsNullOrEmpty(resolved))
                    {
                        return resolved;
                    }
                }
            }
        }
        catch
        {
        }
        return string.Empty;
    }

    private static string ResolveScriptPath(string userProfile)
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;

        // 1. 优先探测当前 exe 所在目录下的 token_hud.py
        if (!string.IsNullOrEmpty(baseDir))
        {
            string localScript = Path.Combine(baseDir, "token_hud.py");
            if (File.Exists(localScript))
            {
                return localScript;
            }

            // 2. 探测同级目录下的 token_hud_path.txt
            string pathTxt = Path.Combine(baseDir, "token_hud_path.txt");
            if (File.Exists(pathTxt))
            {
                try
                {
                    foreach (string line in File.ReadAllLines(pathTxt, Encoding.UTF8))
                    {
                        string resolved = NormalizeCandidatePath(line, baseDir);
                        if (!string.IsNullOrEmpty(resolved))
                        {
                            return resolved;
                        }
                    }
                }
                catch
                {
                }
            }

            // 探测同级目录下的 config.json / token_hud_config.json
            string localCfg = TryReadPathFromJsonConfig(Path.Combine(baseDir, "token_hud_config.json"));
            if (!string.IsNullOrEmpty(localCfg))
            {
                return localCfg;
            }
            string localGenericCfg = TryReadPathFromJsonConfig(Path.Combine(baseDir, "config.json"));
            if (!string.IsNullOrEmpty(localGenericCfg))
            {
                return localGenericCfg;
            }
        }

        // 3. 读取环境变量 TOKEN_HUD_PATH
        string envPath = Environment.GetEnvironmentVariable("TOKEN_HUD_PATH");
        if (!string.IsNullOrWhiteSpace(envPath))
        {
            string resolvedEnv = NormalizeCandidatePath(envPath, baseDir);
            if (!string.IsNullOrEmpty(resolvedEnv))
            {
                return resolvedEnv;
            }
        }

        // 4. 读取 ~/.gemini/token_hud_config.json
        if (!string.IsNullOrEmpty(userProfile))
        {
            string geminiConfig = Path.Combine(userProfile, ".gemini", "token_hud_config.json");
            string resolvedCfg = TryReadPathFromJsonConfig(geminiConfig);
            if (!string.IsNullOrEmpty(resolvedCfg))
            {
                return resolvedCfg;
            }
        }

        // 5. 相对于编译期源码目录进行解析
        string sourceDir = GetCompileTimeSourceDir();
        if (!string.IsNullOrEmpty(sourceDir))
        {
            string sourceScript = Path.Combine(sourceDir, "token_hud.py");
            if (File.Exists(sourceScript))
            {
                return sourceScript;
            }
        }

        return string.Empty;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        if (IsHudAlreadyRunning())
        {
            BringExistingHudToFront();
            return 0;
        }

        string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        string scriptPath = ResolveScriptPath(userProfile);

        if (string.IsNullOrEmpty(scriptPath) || !File.Exists(scriptPath))
        {
            return 1;
        }

        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string candidatePyw = Path.Combine(localAppData, "Python", "bin", "pythonw.exe");
        string exePath;
        StringBuilder sb = new StringBuilder();

        if (File.Exists(candidatePyw))
        {
            exePath = candidatePyw;
            sb.Append(QuoteArg(scriptPath));
        }
        else
        {
            exePath = "pyw.exe";
            sb.Append("-3 ");
            sb.Append(QuoteArg(scriptPath));
        }

        if (args != null)
        {
            for (int i = 0; i < args.Length; i++)
            {
                sb.Append(' ');
                sb.Append(QuoteArg(args[i]));
            }
        }

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = exePath;
        psi.Arguments = sb.ToString();
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.WindowStyle = ProcessWindowStyle.Hidden;
        psi.WorkingDirectory = string.IsNullOrEmpty(userProfile)
            ? (Path.GetDirectoryName(scriptPath) ?? AppDomain.CurrentDomain.BaseDirectory)
            : userProfile;

        try
        {
            using (Process.Start(psi))
            {
            }
            return 0;
        }
        catch
        {
            return 1;
        }
    }
}
