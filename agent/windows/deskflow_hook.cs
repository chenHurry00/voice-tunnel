// Windows 上 Deskflow enterCommand / exitCommand 用的通知程序。
//
// curl.exe 是控制台程序：Deskflow（GUI 进程）用 QProcess::startDetached 启动它时，
// 系统会新建一个控制台窗口直到 curl 退出，每次鼠标进出屏幕都闪一下。
// 本程序编译为 GUI 子系统（/target:winexe），不创建任何窗口。
//
// 构建与 Deskflow 配置见 README 的“快速开始”。用法：
//   hook.exe http://127.0.0.1:8766/active?state=enter&token=<token>
using System.Net;

internal static class DeskflowHook
{
    private static int Main(string[] args)
    {
        if (args.Length != 1)
        {
            return 2;
        }
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(args[0]);
            req.Method = "POST";
            req.ContentLength = 0;
            req.Timeout = 1000;
            req.ReadWriteTimeout = 1000;
            req.Proxy = null;  // 不做系统代理自动探测（WPAD），否则首次请求可能卡数秒
            using (req.GetResponse())
            {
            }
            return 0;
        }
        catch
        {
            return 1;  // 没有窗口可报错；Agent 不在时服务端靠心跳兜底
        }
    }
}
