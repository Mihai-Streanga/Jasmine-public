' Deschide JA.S.Mine. Pornește serverul DOAR dacă nu răspunde deja.
'
' Se ÎNTREABĂ serverul, nu se cronometrează: o așteptare fixă care nu e destulă
' produce un defect care pare al paginii.
'
' Pe Windows, un GET pe un port ÎNCHIS poate bloca ~2 secunde, nu 2 ms. De-aia
' se întreabă întâi PORTUL, ieftin, și abia dacă e deschis se întreabă SERVERUL.
' Un port închis dovedește că serverul e oprit; unul deschis nu dovedește nimic,
' deci după el urmează apelul adevărat la `/api/viu`.
'
' Căile se calculează din locul scriptului: depozitul poate sta oriunde.

Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Dim folderServer, radacina
folderServer = fso.GetParentFolderName(WScript.ScriptFullName)
radacina = fso.GetParentFolderName(folderServer)
ws.CurrentDirectory = folderServer

' Motorul de conversație, ÎNAINTE de orice așteptare. Open WebUI rulează în
' Docker, iar Docker pornește odată cu JA.S.Mine, nu la logon. Nu se așteaptă:
' fereastra se deschide rece, iar COMMAND și AGORA se aprind singure când Sky
' poate răspunde.
'
' `-Autostart` și nu `docker desktop start`: comanda din CLI deschide și
' fereastra Docker Dashboard, care sare peste kiosk. `-Autostart` pornește
' motorul fără interfață — dar lansat peste un Docker care merge deja, DESCHIDE
' fereastra. De-asta se întreabă întâi (`DockerPornit()`).
If Not DockerPornit() Then
  ws.Run """C:\Program Files\Docker\Docker\Docker Desktop.exe"" -Autostart", 0, False
End If

' Portul întâi (~0,1 s), serverul după.
Dim portTinut, viu
portTinut = PortDeschis()
viu = False
If portTinut Then viu = Raspunde()

If Not viu Then
  ' Curățenia se face NUMAI dacă portul e chiar ținut — de un server pe
  ' jumătate mort, care ascultă și nu mai răspunde.
  If portTinut Then
    ws.Run "cmd /c for /f ""tokens=5"" %a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %a", 0, True
    WScript.Sleep 500
  End If
  ws.Run "py server.py", 0
End If

' Pregătește profilul kiosk-ului ÎNAINTE de Chrome: stinge bula „traduci
' pagina?", scrie permisiunea de microfon și șterge marcajul de „a crăpat".
' Se așteaptă finalizarea (True): pornit după Chrome, reglajul s-ar aplica abia
' la pornirea următoare. `--disable-features=Translate` NU stinge bula; pârghia
' e preferința profilului, de-asta e un script și nu un flag.
'
' Cu kiosk-ul deja deschis, scriptul n-ar face nimic, deci nu se lansează.
If Not KioskViu() Then ws.Run "py pregateste-chrome.py", 0, True

' Se așteaptă până răspunde, cel mult 60 de secunde. Dacă nici atunci, Chrome se
' deschide oricum: puntea are propriul mesaj pentru un server care lipsește.
' Plafonul se ține pe CEAS, nu pe numărul de rotiri: o rotire poate costa 0,15 s
' sau 2,2 s, după cum răspunde portul.
Dim tPanda, scurs
tPanda = Timer
Do
  If PortDeschis() Then
    If Raspunde() Then Exit Do
  End If
  WScript.Sleep 50
  scurs = Timer - tPanda
  If scurs < 0 Then scurs = scurs + 86400      ' Timer se întoarce la zero la miezul nopții
Loop While scurs < 60

' Deschide puntea — DAR NUMAI DACĂ NU E DEJA UNA DESCHISĂ.
'
' A doua lansare de Chrome pe același `--user-data-dir` nu aduce în față
' fereastra veche: face o A DOUA FEREASTRĂ în același proces. Două pagini vii
' înseamnă două urechi pe același microfon și două clipuri în MUNTELE. Serverul
' vede o singură fereastră per proces, deci a doua n-ar mai putea fi nici
' minimizată, nici închisă. Se apără AICI, unde se naște.
'
' Deschisă deja, se RIDICĂ: serverul o scoate din bară dacă e minimizată.
'
' `--autoplay-policy` e necesar: camera MUNTELE se poate deschide cu vocea, fără
' niciun gest de om în pagină, iar fără flag `video.play()` e refuzat.
If KioskViu() Then
  RidicaPuntea
Else
  ws.Run """C:\Program Files\Google\Chrome\Application\chrome.exe"" --kiosk --app=http://localhost:8000 --user-data-dir=""" & radacina & "\chrome-profil"" --no-first-run --no-default-browser-check --autoplay-policy=no-user-gesture-required", 1
End If

' Rulează Docker Desktop? Se caută PROCESUL, nu se cheamă `docker desktop
' status`, care costă secunde bune pe un Docker oprit. Fals la orice eroare: o
' lansare în plus costă o fereastră, nu o stricăciune.
Function DockerPornit()
  Dim wmi, lista
  DockerPornit = False
  On Error Resume Next
  Set wmi = GetObject("winmgmts:\\.\root\cimv2")
  Set lista = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='Docker Desktop.exe'")
  If Err.Number = 0 Then
    If lista.Count > 0 Then DockerPornit = True
  End If
  Err.Clear
  On Error GoTo 0
End Function

' Rulează un Chrome PE PROFILUL kiosk-ului? Se caută după LINIA DE COMANDĂ,
' niciodată după numele imaginii: un Chrome obișnuit poate fi deschis alături și
' n-are nicio treabă cu kiosk-ul. Semnătura e folderul profilului — același tipar cu
' `SEMNATURA_KIOSK` din `server.py` și cu `chrome_viu()` din
' `pregateste-chrome.py`.
'
' Fals la orice eroare, și aici direcția e cea sigură: fals înseamnă „lansează
' scriptul", iar scriptul are garda lui, care la nesiguranță nu scrie nimic.
Function KioskViu()
  Dim wmi, lista
  KioskViu = False
  On Error Resume Next
  Set wmi = GetObject("winmgmts:\\.\root\cimv2")
  Set lista = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='chrome.exe' AND CommandLine LIKE '%chrome-profil%'")
  If Err.Number = 0 Then
    If lista.Count > 0 Then KioskViu = True
  End If
  Err.Clear
  On Error GoTo 0
End Function

' Ascultă cineva pe 8000? Costă ~0,1 s și e singurul mod de a nu plăti cele 2
' secunde ale unei întrebări puse unui port închis.
'
' `netstat` și nu WMI: `MSFT_NetTCPConnection` raportează portul deschis cu
' câteva secunde întârziere, iar pânda ar aștepta un server pornit deja.
'
' ADEVĂRAT la orice eroare: nu se poate ști înseamnă „poate ascultă", deci se
' pune întrebarea adevărată mai departe. Vechea purtare, cu costul ei.
Function PortDeschis()
  Dim cod
  PortDeschis = True
  On Error Resume Next
  cod = ws.Run("cmd /c netstat -ano | findstr :8000 | findstr LISTENING", 0, True)
  If Err.Number = 0 Then PortDeschis = (cod = 0)
  Err.Clear
  On Error GoTo 0
End Function

' Scoate din bară fereastra deschisă. Nu e sondă și nu așteaptă nimic: dacă
' serverul nu răspunde, omul are oricum fereastra pe ecran undeva.
Sub RidicaPuntea()
  Dim h
  On Error Resume Next
  Set h = CreateObject("MSXML2.XMLHTTP")
  h.Open "POST", "http://127.0.0.1:8000/api/ridica-puntea", False
  h.Send
  Err.Clear
  On Error GoTo 0
End Sub

' Întreabă serverul dacă e viu. Fals la orice — port închis, eroare, alt cod.
' `/api/viu`, nu `/api/stare`: al doilea sondează și Open WebUI și agentul 051,
' iar cu Docker încă în pornire apelurile alea expiră.
Function Raspunde()
  Dim h
  Raspunde = False
  On Error Resume Next
  Set h = CreateObject("MSXML2.XMLHTTP")
  h.Open "GET", "http://127.0.0.1:8000/api/viu", False
  h.Send
  If Err.Number = 0 Then
    If h.Status = 200 Then Raspunde = True
  End If
  Err.Clear
  On Error GoTo 0
End Function
