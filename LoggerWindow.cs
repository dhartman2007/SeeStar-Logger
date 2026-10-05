using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;
using System.Web.Script.Serialization;

public class LoggerWindow : Form {
    string root=AppDomain.CurrentDomain.BaseDirectory;
    string python=Environment.GetEnvironmentVariable("SEESTAR_PYTHON") ?? "python";
    string logs,stopPath; int baseline=0,ticks=0; bool closing=false,stopping=false,started=false;
    Process logger,statusJob; ConcurrentQueue<string> output=new ConcurrentQueue<string>();
    TextBox folder=new TextBox(); Label state=new Label(),details=new Label();
    Button start=new Button(),stop=new Button(),browse=new Button();
    RichTextBox console=new RichTextBox(); Dictionary<string,Label> numbers=new Dictionary<string,Label>();
    System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer();
    Label LabelAt(string text,int x,int y,int w,int h) {Label l=new Label();l.Text=text;l.SetBounds(x,y,w,h);Controls.Add(l);return l;}
    Button ButtonAt(string text,int x,int width) {Button b=new Button();b.Text=text;b.SetBounds(x,90,width,34);Controls.Add(b);return b;}
    public LoggerWindow() {
        logs=Path.Combine(root,"logs");Text="Seestar Observing Logger";Size=new Size(1120,850);MinimumSize=new Size(1000,750);StartPosition=FormStartPosition.CenterScreen;Font=new Font("Segoe UI",10);
        Label title=LabelAt("Seestar Observing Logger",20,15,800,35);title.Font=new Font("Segoe UI",20,FontStyle.Bold);
        state=LabelAt("Stopped",20,58,1040,25);LabelAt("Image folder",20,96,100,25);
        folder.Text=@"Z:\MyWorks";folder.SetBounds(125,92,520,30);Controls.Add(folder);
        browse=ButtonAt("Browse",655,85);start=ButtonAt("Start",750,85);stop=ButtonAt("Stop",845,85);stop.Enabled=false;
        Button open=ButtonAt("Open logs",940,110);
        LabelAt("Unique candidate objects in sessions created after Start. Existing images form the baseline.",20,140,1040,24);
        string[] names={"Aircraft","Satellites","Comets","Asteroids","Meteors"};
        for(int i=0;i<names.Length;i++) {GroupBox box=new GroupBox();box.Text=names[i];box.SetBounds(20+210*i,175,198,85);Controls.Add(box);Label n=new Label();n.Text=i==4?"Not available":"Waiting";n.SetBounds(10,27,180,40);n.Font=new Font("Segoe UI",i==4?12:23,FontStyle.Bold);box.Controls.Add(n);numbers[names[i]]=n;}
        LabelAt("Aircraft/satellites: possible FOV crossings only. Comets/asteroids: predicted search-field candidates.\r\nMeteor detection is not implemented. Zero candidates does not establish coverage or confirm a clear image.",20,275,1040,50);
        details=LabelAt("Waiting for logger data.",20,333,1040,155);LabelAt("Logger output (same as CLI)",20,498,1040,25);
        console.SetBounds(20,525,1060,265);console.Anchor=AnchorStyles.Top|AnchorStyles.Bottom|AnchorStyles.Left|AnchorStyles.Right;console.ReadOnly=true;console.BackColor=Color.FromArgb(22,32,43);console.ForeColor=Color.FromArgb(229,238,247);console.Font=new Font("Consolas",10);Controls.Add(console);
        browse.Click+=(s,e)=>{using(FolderBrowserDialog d=new FolderBrowserDialog()){d.SelectedPath=folder.Text;if(d.ShowDialog()==DialogResult.OK)folder.Text=d.SelectedPath;}};
        open.Click+=(s,e)=>{Directory.CreateDirectory(logs);Process.Start("explorer.exe",Quote(logs));};
        start.Click+=(s,e)=>StartLogger();stop.Click+=(s,e)=>RequestStop();
        FormClosing+=(s,e)=>{if(logger!=null){e.Cancel=true;closing=true;RequestStop();state.Text="Closing after logger shutdown — waiting for scans/workers";}};
        timer.Interval=500;timer.Tick+=(s,e)=>Tick();timer.Start();
    }
    string Quote(string text) {return "\""+text.TrimEnd('\\')+"\"";}
    Process Python(string args) {Process p=new Process();p.StartInfo=new ProcessStartInfo(python,args);p.StartInfo.WorkingDirectory=root;p.StartInfo.UseShellExecute=false;p.StartInfo.CreateNoWindow=true;p.StartInfo.RedirectStandardOutput=true;p.StartInfo.RedirectStandardError=true;p.StartInfo.StandardOutputEncoding=Encoding.UTF8;p.StartInfo.StandardErrorEncoding=Encoding.UTF8;p.StartInfo.EnvironmentVariables["PYTHONIOENCODING"]="utf-8";return p;}
    void Write(string text) {console.AppendText(text);if(console.TextLength>250000){console.Select(0,50000);console.SelectedText="";}console.SelectionStart=console.TextLength;console.ScrollToCaret();}
    void StartLogger() {
        if(logger!=null)return;
        try {
            if(folder.Text.Contains("\""))throw new Exception("Folder paths cannot contain quotation marks.");
            Directory.CreateDirectory(logs);
            using(Process b=Python("-u gui_status.py --directory "+Quote(logs)+" --baseline")){b.Start();string data=b.StandardOutput.ReadToEnd();b.WaitForExit();if(b.ExitCode!=0)throw new Exception(b.StandardError.ReadToEnd());baseline=int.Parse(data.Trim());}
            stopPath=Path.Combine(logs,"gui_stop_"+Guid.NewGuid().ToString("N")+".flag");
            Process p=Python("-u file_logger.py --folder "+Quote(folder.Text)+" --output "+Quote(logs)+" --stop-file "+Quote(stopPath));
            p.OutputDataReceived+=(s,e)=>{if(e.Data!=null)output.Enqueue(e.Data+"\r\n");};p.ErrorDataReceived+=(s,e)=>{if(e.Data!=null)output.Enqueue(e.Data+"\r\n");};
            p.Start();p.BeginOutputReadLine();p.BeginErrorReadLine();logger=p;stopping=false;started=true;
            start.Enabled=false;stop.Enabled=true;browse.Enabled=false;folder.Enabled=false;
            state.Text="Running — building baseline / watching for changes";Write("\r\n--- Logger started ---\r\n");
        }catch(Exception error){MessageBox.Show(error.Message,"Unable to start logger");}
    }
    void RequestStop() {if(logger!=null&&!logger.HasExited&&!stopping){File.WriteAllText(stopPath,"stop");stopping=true;stop.Enabled=false;state.Text="Stopping — waiting for scans and workers to finish";}}
    void Tick() {
        string line;for(int i=0;i<200&&output.TryDequeue(out line);i++)Write(line);
        if(logger!=null&&logger.HasExited){logger.WaitForExit();while(output.TryDequeue(out line))Write(line);int code=logger.ExitCode;logger.Dispose();logger=null;state.Text="Stopped (exit code "+code+")";start.Enabled=true;stop.Enabled=false;browse.Enabled=true;folder.Enabled=true;if(File.Exists(stopPath))File.Delete(stopPath);Write("--- Logger exited: "+code+" ---\r\n");if(closing){Close();return;}}
        if(statusJob!=null&&statusJob.HasExited){try{string json=statusJob.StandardOutput.ReadToEnd();var data=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(json);var counts=(Dictionary<string,object>)data["counts"];foreach(string key in numbers.Keys)numbers[key].Text=Convert.ToString(counts[key]);details.Text=Convert.ToString(data["details"]).Replace("\n","\r\n");}catch{details.Text="Status temporarily unavailable; see logger output.";}statusJob.Dispose();statusJob=null;}
        if(++ticks>=4&&statusJob==null&&started){ticks=0;try{statusJob=Python("-u gui_status.py --directory "+Quote(logs)+" --after "+baseline);statusJob.Start();}catch{if(statusJob!=null)statusJob.Dispose();statusJob=null;}}
    }
    protected override void Dispose(bool disposing){if(disposing){timer.Stop();timer.Dispose();if(statusJob!=null){statusJob.WaitForExit();statusJob.Dispose();}}base.Dispose(disposing);}
    [STAThread] public static void Main(string[] args){Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);using(LoggerWindow window=new LoggerWindow()){if(args.Length>0&&args[0]=="--smoke-test")return;Application.Run(window);}}
}
