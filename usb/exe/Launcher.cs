// KwickBoot USB Creator - single-exe launcher (GUI). Embeds kwickboot-usb.ps1 as a resource and (optionally) the ISO
// appended to the exe. Runs the script elevated via its own UAC manifest, with no console window.
// SPDX-License-Identifier: AGPL-3.0-or-later
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("KwickBoot USB Creator")]
[assembly: AssemblyCompany("Jins Tech Corp")]
[assembly: AssemblyProduct("KwickBoot")]
[assembly: AssemblyDescription("Creates a KwickBoot USB stick")]
[assembly: AssemblyCopyright("Jins Tech Corp - AGPL-3.0-or-later")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

class KwickBootUSB
{
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            // the exe's real path on disk (it may have the ISO appended); the script reads it via KBUSB_EXE
            Environment.SetEnvironmentVariable("KBUSB_EXE", Process.GetCurrentProcess().MainModule.FileName);

            var asm = Assembly.GetExecutingAssembly();
            string res = null;
            foreach (var n in asm.GetManifestResourceNames())
                if (n.EndsWith("kwickboot-usb.ps1", StringComparison.OrdinalIgnoreCase)) { res = n; break; }
            if (res == null) { MessageBox.Show("Embedded script not found.", "KwickBoot USB Creator"); return 3; }

            string tmp = Path.Combine(Path.GetTempPath(), "KwickBootUSB-" + Guid.NewGuid().ToString("N") + ".ps1");
            using (var s = asm.GetManifestResourceStream(res))
            using (var f = File.Create(tmp))
                s.CopyTo(f);

            // extract the embedded no-PXE boot image (if this exe was built with one) so the script can raw-write it
            string npImg = null;
            foreach (var n in asm.GetManifestResourceNames())
                if (n.EndsWith("kwickboot-boot.img", StringComparison.OrdinalIgnoreCase))
                {
                    npImg = Path.Combine(Path.GetTempPath(), "KwickBootNoPxe-" + Guid.NewGuid().ToString("N") + ".img");
                    using (var s2 = asm.GetManifestResourceStream(n))
                    using (var f2 = File.Create(npImg))
                        s2.CopyTo(f2);
                    Environment.SetEnvironmentVariable("KBUSB_NOPXE_IMG", npImg);
                    break;
                }

            // A normal double-click opens the GUI.  Explicit command-line arguments are
            // used by automation and by the GUI's worker process, so do not force -Gui
            // in that case (it would otherwise override -Console/-Disk and block writes).
            string pass = args.Length == 0 ? " -Gui" : "";
            foreach (var a in args) pass += " \"" + a.Replace("\"", "\\\"") + "\"";
            var psi = new ProcessStartInfo("powershell.exe",
                "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + tmp + "\"" + pass)
            { UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden };
            var p = Process.Start(psi);
            p.WaitForExit();
            try { File.Delete(tmp); } catch { }
            try { if (npImg != null) File.Delete(npImg); } catch { }
            return p.ExitCode;
        }
        catch (Exception e)
        {
            MessageBox.Show("KwickBoot USB Creator failed to start:\n" + e.Message, "KwickBoot USB Creator");
            return 2;
        }
    }
}
