# Kamil V1 — basit uzak masaüstü

İki bilgisayarı (farklı şehirlerde, ev modemi/NAT arkasında olsa da) internet üzerinden bağlayan, çok basit bir
TeamViewer/AnyDesk benzeri araç. **Host** ekranını paylaşır, **Client** bağlanıp fareyle/klavyeyle yönetir.

Tek bir `KamilV1.exe` dosyası (`setup.exe` olarak da kopyalanır). **Taşınabilir programdır, kurulum sihirbazı
değildir:** çift tıklayınca program direkt açılır; kurulum ekranı, Başlat menüsü kısayolu veya "Program
Ekle/Kaldır" girişi yoktur. Kaldırmak için exe'yi silmek yeterlidir (ayarlar `%LOCALAPPDATA%\Kamil V1\` içinde
kalır, istersen o klasörü de sil). Karşı bilgisayarda Python kurulu olmasına **gerek yoktur** — gereken her şey
(Python çalışma zamanı, Tkinter, Pillow, mss, cryptography, pyngrok) exe'nin içine gömülüdür.

## Güvenlik duyurusu (önemli)

Bu, bağımsız denetimden geçmiş bir ürün değildir — kişisel kullanım içindir. Protokol X25519 + HKDF (parola
türevli) + ChaCha20-Poly1305 kullanır, **PAKE değildir**: yolun üzerinde oturan VE bir tarafı taklit eden biri
(ngrok hizmeti, kendi relay sunucunuzu işleten biri, veya trafiği başka yere yönlendirebilen biri) tek bir el
sıkışma karesini yakalayıp parolayı **çevrimdışı** tahmin etmeye çalışabilir. Bu yüzden parola gücü önemlidir:

- Varsayılan (üretilen) parola 12 sembol, ~59 bit — pratikte kırılamaz.
- Kendi parolanızı yazarsanız **en az 16 karakter** şart (arayüz daha kısasını kabul etmez).
- Üretilen parolayı değiştirmemeniz önerilir.

Ciddi/kurumsal kullanım için bunu bir VPN'in arkasına koyun; bu araç onun yerine geçmez.

## Hızlı başlangıç

İki yol var — biri her şeyi otomatik halleder ama aylık veri sınırı küçük, diğeri sınırsız ama kendi sunucunuz
gerekir.

### Yol 1 — ngrok (ekstra kurulum gerektirmez, varsayılan)

1. Programı aç, **"Bu Bilgisayarın İzlenmesine İzin Ver (Sunucu/Host)"**'u seç.
2. Ngrok modu seçiliyken, görünen linkten (dashboard.ngrok.com) ücretsiz bir hesap açıp kendi **Authtoken**'ını
   kopyala, ilgili kutuya yapıştır. (Not: ngrok'un ücretsiz TCP uç noktası için hesabına bir kredi/banka kartı
   eklemesi gerekiyor — kart **tahsilat için değil**, doğrulama için isteniyor.)
3. **"Hazır Ol / Başlat"**'a bas. Birkaç saniye içinde bir **Bağlantı ID'si** (örn. `0.tcp.eu.ngrok.io:12345`)
   ve bir **Şifre** görünür. İkisini de karşı taraftaki kişiye güvenli bir kanaldan (telefon, mesaj) ilet.
4. Diğer bilgisayarda programı aç, **"Uzaktaki Bilgisayarı Yönet (İstemci/Client)"**'i seç, ID ve şifreyi
   yazıp **"Bağlan"**'a bas.
5. Host tarafında bir onay penceresi çıkar ("İzin ver ve hatırla" / "Reddet"). İlk bağlantıda biri bu düğmeye
   basmalı. Kabul edilen cihaz bir daha hatırlanır: **sonraki bağlantılarda onay penceresi çıkmaz**, otomatik
   kabul edilir.

**Ngrok'un ücretsiz plan sınırı:** ayda ~1 GB veri. Bir kare ortalama ~60-75 KB olduğundan bu, sürekli hareketli
bir ekranda yaklaşık **20-25 dakika** sürer (durağan ekranlarda çok daha uzun gider, çünkü değişmeyen kareler
gönderilmez). Host penceresindeki "Gönderilen" sayacı 800 MB'ı geçince uyarı rengine döner. Düzenli/uzun süreli
kullanım için Yol 2'yi tercih edin.

### Yol 2 — kendi relay sunucunuz (sınırsız, sabit ID, önerilen)

Herhangi bir ucuz Linux/Windows VPS (public IP'si olan) yeterli. Python 3.8+ olması yeterli, başka hiçbir şey
kurmaya gerek yok — `relay.py` tek başına, sadece standart kütüphane ile çalışır.

1. `relay.py` dosyasını VPS'e kopyala, çalıştır:
   ```
   python relay.py --port 9000 --host 0.0.0.0
   ```
   Token vermezsen rastgele üretilir ve ekrana yazılır — onu not al. VPS'in güvenlik duvarında/kontrol
   panelinde **9000 portunu (TCP)** dışarıya aç.
2. Her iki bilgisayarda programda Host modunu **"Kendi relay sunucum"** yap; relay adresini (`vps-ip:9000`) ve
   tokenı yazıp kaydet. Host'taki **Bağlantı ID'si** artık sabit 9 haneli bir numaradır, her açılışta değişmez.
3. Gerisi Yol 1 ile aynı: ID + şifreyi paylaş, Client'ta bağlan, ilk bağlantıda Host'ta onay ver.

Bu yolda veri sınırı yoktur, ID değişmez; tek dezavantajı kendi sunucunuzu ayakta tutmanız gerekmesidir.

## Güvenlik ve kararlılık notları

- **Tek seferlik onay:** bir cihaz bir kez kabul edildiğinde parola/şifre bir daha sorulmaz, bağlantı otomatik
  kurulur. Güvendiğiniz cihazları Host panelindeki **"Güvenilenleri Sıfırla"** ile silebilirsiniz.
- **Hiçbir şey gizlenmiyor:** Host penceresi her zaman görünür, kim bağlandığını gösterir, "Bağlantıyı Kes"
  düğmesi vardır. Program arkada sessizce/görünmeden çalışmaz; bu bilerek böyle tasarlandı.
- **Pencere asla kilitlenip kalmaz:** kapat düğmesine basınca program en çok 5 saniye içinde kapanır (arka plan
  iş parçacıkları "daemon" olduğu için süreç onları beklemeden çıkabilir), `os._exit` gibi sert bir çıkış yoktur.
- **Basılı kalan tuş:** normal kapanışta/kopmada Host, basılı kalan her tuş ve fare düğmesini otomatik bırakır.
  Tek istisna: Host programı Görev Yöneticisi'nden zorla sonlandırılır veya bilgisayarın elektriği kesilirse, o
  anda basılı gönderilmiş bir tuş Windows'ta "basılı" görünebilir — ilgili tuşa bir kez fiziksel olarak
  dokununca kendiliğinden düzelir. Bu, bu tür araçların (TeamViewer dahil) ortak bir sınırıdır.
- **Uyku engeli kalıcı değil:** Host "Hazır" durumdayken bilgisayarın uykuya geçmesi engellenir (bağlantı
  kopmasın diye); Host durunca bu kalkar. Program çökse bile Windows bu durumu otomatik temizler.
- **İletilemeyenler:** Win tuşu, Alt+Tab, Ctrl+Alt+Del — bunlar sizin kendi bilgisayarınızda Windows tarafından
  yakalanır, uzak bilgisayara gönderilemez. Yazdığınız harfler (Türkçe dahil: ş ğ ü ö ç ı İ) karşı tarafın
  klavye düzeninden bağımsız doğru çıkar.
- **Kilitli ekran / UAC:** Host ekranı kilitliyse veya bir UAC izni penceresi açıksa, görüntü donar ve
  görüntüleyicide "Host ekranı kilitli / UAC açık" yazar; o pencereye tıklama/yazı gönderilemez (Windows'un
  güvenli masaüstü koruması). Gözetimsiz bir Host için ekran kilidini/uyku modunu Windows ayarlarından kapatın.
- **Tek seferde bir görüntüleyici:** Host aynı anda yalnızca bir Client'a bağlanabilir.

## Kaynaktan çalıştırma (geliştirme)

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

Testler:
```
.venv\Scripts\pip install pytest
.venv\Scripts\python -m pytest -q
```
(Gerçek fare/klavye gönderen opsiyonel test varsayılan olarak atlanır; çalıştırmak güvenli değildir, bu depoda
hiçbir otomasyon onu tetiklemez.)

## .exe üretme

```
build.bat
```
Bu betik: `.venv` yoksa oluşturur, `requirements.txt` + PyInstaller'ı kurar, tek dosyalık, konsolsuz bir exe
derler, `dist\KamilV1.exe` üretir ve onu `dist\setup.exe` olarak da kopyalar. Elle aynı işi yapmak isterseniz:

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --noupx --onefile --noconsole --name KamilV1 main.py
copy dist\KamilV1.exe dist\setup.exe
```

`dist\KamilV1.exe --selftest` çalıştırılırsa ağa hiç çıkmadan (ngrok'a bağlanmadan, gerçek girdi göndermeden)
içe aktarma, şifreleme, el sıkışma, ekran yakalama ve Tk'nin çalıştığını doğrulayıp bir sonuç dosyası yazar;
`0` çıkış kodu = hepsi geçti.

## İlk çalıştırmada karşılaşabilecekleriniz

- **SmartScreen "Bilinmeyen yayımcı" uyarısı:** exe imzasız olduğu için normaldir. "Daha fazla bilgi" →
  "Yine de çalıştır" ile devam edin.
- **Windows Güvenliği → Uygulama ve tarayıcı denetimi → Akıllı Uygulama Denetimi** açıksa imzasız exe'yi
  engelleyebilir; kapatmanız veya istisna eklemeniz gerekebilir.
- Antivirüs programları uzaktan-kontrol araçlarını (ekran yakalama + girdi enjeksiyonu + ağ) temkinli
  karşılayabilir; bu beklenen bir durumdur, kötü amaçlı kod değildir. Şüpheli bulursanız exe'nin SHA-256'sını
  karşılaştırın (kendi derlediğiniz exe'nin özetini `certutil -hashfile dist\KamilV1.exe SHA256` ile alabilirsiniz).
- `--onedir` (klasörlü) derleme SmartScreen/AV ile sorun yaşarsanız alternatif olabilir: `build.bat` içindeki
  `--onefile` bayrağını `--onedir` ile değiştirip tekrar çalıştırın; sonuçta `dist\KamilV1\` klasörü oluşur.

## ngrok gerçek bağlantı kontrol listesi (bu depoda otomatik test edilmez)

Gerçek ngrok'a hiçbir otomatik test bağlanmaz (tasarım gereği). Kurulumdan sonra elle 5 dakikalık bir kontrol
önerilir: (1) Authtoken'ı gir, Başlat'a bas, ID'nin birkaç saniye içinde göründüğünü doğrula; (2) ikinci bir
bilgisayardan (veya aynı bilgisayarda ikinci bir kopyadan) o ID+şifre ile bağlan; (3) Host'ta onay penceresinin
çıktığını, kabul edince görüntünün geldiğini doğrula; (4) fare hareketi/tıklama ve Türkçe karakterli yazı
gönderip Host'ta uygulandığını gözle kontrol et; (5) Host'u durdurup tekrar başlat, aynı şifreyle (farklı ID
ile) tekrar bağlanabildiğini ve onay penceresinin artık çıkmadığını doğrula.

## Bilinen sınırlar

- Yalnızca birincil monitör paylaşılır, çoklu monitör seçimi yoktur.
- Pano (kopyala/yapıştır) senkronizasyonu, dosya transferi, ses yoktur.
- Oyun gibi ham tuş kodu okuyan uygulamalar, yazılan harfleri (Unicode metin olarak gönderildiği için)
  göremeyebilir; kısayollar (Ctrl+C vb.) yine de çalışır.
- Düzen tarayıcı/program yalnızca bu bilgisayarda saklanır; ayarlar `%LOCALAPPDATA%\Kamil V1\config.json`
  içindedir ve `k` (parola türevi anahtarı), `uid` ve `trusted` listesi parola kadar hassastır — bu dosyaya
  ayrı bir şifreleme uygulanmaz (aynı Windows kullanıcısı olarak çalışan kötü amaçlı yazılım zaten okuyabilir).
