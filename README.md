# Kamil V1 — basit uzak masaüstü

İki bilgisayarı (farklı şehirlerde, ev modemi/NAT arkasında olsa da) internet üzerinden bağlayan, çok basit bir
TeamViewer/AnyDesk benzeri araç. **Host** ekranını paylaşır, **Client** bağlanıp fareyle/klavyeyle yönetir.
**Hesap, kart ya da abonelik gerekmez.**

Tek bir `KamilV1.exe` dosyası (`setup.exe` olarak da kopyalanır). **Taşınabilir programdır, kurulum sihirbazı
değildir:** çift tıklayınca program direkt açılır; kurulum ekranı, Başlat menüsü kısayolu veya "Program
Ekle/Kaldır" girişi yoktur. Kaldırmak için exe'yi silmek yeterlidir (ayarlar `%LOCALAPPDATA%\Kamil V1\` içinde
kalır, istersen o klasörü de sil). Karşı bilgisayarda Python kurulu olmasına **gerek yoktur** — gereken her şey
(Python çalışma zamanı, Tkinter, Pillow, mss, cryptography, pyngrok) exe'nin içine gömülüdür.

## Güvenlik duyurusu (önemli)

Bu, bağımsız denetimden geçmiş bir ürün değildir — kişisel kullanım içindir. Protokol X25519 + HKDF (parola
türevli) + ChaCha20-Poly1305 kullanır, **PAKE değildir**: yolun üzerinde oturan VE bir tarafı taklit eden biri
(tünel sunucusunu işleten, kendi relay sunucunuzu işleten ya da trafiği başka yere yönlendirebilen biri) tek bir
el sıkışma karesini yakalayıp parolayı **çevrimdışı** tahmin etmeye çalışabilir. Bu yüzden parola gücü önemlidir:

- Varsayılan (üretilen) parola 12 sembol, ~59 bit — pratikte kırılamaz.
- Kendi parolanızı yazarsanız **en az 16 karakter** şart (arayüz daha kısasını kabul etmez).
- Üretilen parolayı değiştirmemeniz önerilir.

Ciddi/kurumsal kullanım için bunu bir VPN'in arkasına koyun; bu araç onun yerine geçmez.

## Hızlı başlangıç

Üç bağlantı yolu var. Hepsinde veri uçtan uca şifrelidir; aradaki sunucu içeriği göremez.

### Yol 1 — Ücretsiz tünel (varsayılan, hesap yok)

1. Programı aç, **"Bu Bilgisayarın İzlenmesine İzin Ver (Sunucu/Host)"**'u seç. Yöntem olarak **"Ücretsiz tünel
   (hesap yok)"** seçili gelir; başka hiçbir şey girmen gerekmez.
2. **"Hazır Ol / Başlat"**'a bas. Birkaç saniye içinde bir **Bağlantı ID'si** (örn. `bore.pub:27750`) ve bir
   **Şifre** görünür. İkisini de karşı taraftaki kişiye güvenli bir kanaldan (telefon, mesaj) ilet.
3. Diğer bilgisayarda programı aç, **"Uzaktaki Bilgisayarı Yönet (İstemci/Client)"**'i seç, ID ve şifreyi
   yazıp **"Bağlan"**'a bas.
4. Host tarafında bir onay penceresi çıkar ("İzin ver ve hatırla" / "Reddet"). İlk bağlantıda biri bu düğmeye
   basmalı (bkz. "Süresiz / katılımsız bağlanma").

Bu yol, herkese açık ücretsiz bir `bore` sunucusu (varsayılan `bore.pub`, açık kaynak
[ekzhang/bore](https://github.com/ekzhang/bore)) üzerinden çalışır. Bilmen gerekenler:

- Sunucu **başkasına aittir**; çalışma süresi, hız ya da kalıcılık garantisi yoktur. Kapanırsa ya da kısıtlarsa bu
  yol çalışmaz; o zaman Yol 2'ye geç. İsterseniz "Tünel sunucusu" kutusuna başka bir bore sunucusu yazabilirsiniz.
- Sunucu içeriği göremez (uçtan uca şifreli) ama IP adreslerinizi, bağlantı zamanını ve veri miktarını görebilir.
- ID, sunucunun verdiği **genel port**tur. Program yeniden başlayınca aynı portu yeniden ister; boşsa ID değişmez,
  doluysa yeni bir ID verilir (her seferinde ID'ye bakın).
- Veri miktarı ya da bağlantı süresi için bu programda bir sınır yoktur.

### Yol 2 — kendi relay sunucunuz (sabit ID, en güvenilir)

Herhangi bir ucuz Linux/Windows VPS (public IP'si olan) yeterli. Python 3.8+ olması yeterli, başka hiçbir şey
kurmaya gerek yok — `relay.py` tek başına, sadece standart kütüphane ile çalışır.

1. `relay.py` dosyasını VPS'e kopyala, çalıştır:
   ```
   python relay.py --port 9000 --host 0.0.0.0
   ```
   Token vermezsen rastgele üretilir ve ekrana yazılır — onu not al. VPS'in güvenlik duvarında/kontrol
   panelinde **9000 portunu (TCP)** dışarıya aç.
2. Host panelinde yöntemi **"Kendi relay sunucum"** yap; relay adresini (`vps-ip:9000`) ve tokenı yaz.
   Host'taki **Bağlantı ID'si** artık sabit 9 haneli bir numaradır, her açılışta değişmez. Client'ta aynı relay
   adresi/token "Gelişmiş" bölümüne yazılır.
3. Gerisi Yol 1 ile aynı.

### Yol 3 — ngrok (isteğe bağlı; hesap + kart gerekir)

Yöntem olarak **"ngrok (hesap + kart)"** seçilirse kullanılabilir. Ücretsiz bir ngrok hesabı açıp Authtoken'ı
yapıştırmanız gerekir. Ücretsiz TCP uç noktası için hesaba kart eklenmesi istenir (doğrulama içindir, tahsilat
yok) ve ücretsiz planda ayda ~1 GB veri sınırı vardır: bir kare ortalama ~60-75 KB olduğundan bu, sürekli
hareketli bir ekranda yaklaşık **20-25 dakika** sürer. Host penceresindeki "Gönderilen" sayacı 800 MB'ı geçince
uyarı rengine döner. Hesapsız yollar varken bunu seçmenizi önermeyiz.

## Süresiz / katılımsız bağlanma

- **İlk bağlantıda** Host'un başındaki biri onay penceresinde **"İzin ver ve hatırla"** der. Bundan sonra aynı
  Client bilgisayarı **sormadan** bağlanır; süre sınırı yoktur. Şifre de değişmez: Host şifreyi kendi ayar
  dosyasında anahtar olarak saklar (şifrenin kendisini değil), yeniden başlatsanız da aynı kalır.
- **Hatırlama silinir:** Host'ta **"Bağlantıyı Kes"** düğmesi o cihazın hatırlanmasını da siler (sonraki bağlantı
  yeniden onay ister); **"Güvenilenleri Sıfırla"** hepsini siler; Client bilgisayarın ayar klasörü silinirse
  Client yeni bir kimlik alır ve yeniden onay gerekir.
- **Programın çalışması gerekir:** Host bir sistem hizmeti değildir, Windows ile kendiliğinden başlamaz. Host
  penceresi açık ve "Hazır" durumdayken bağlanılabilir (bu sırada bilgisayarın uykuya geçmesi engellenir).
  Bilgisayar kapalıysa, program kapalıysa, ekran kilitliyse ya da UAC penceresi açıksa bağlantı kurulamaz/ekran donar.
- **ID değişebilir:** ücretsiz tünelde Host her açılışta aynı portu ister ama garanti değildir; relay'de ID sabittir.
  Katılımsız kullanım için Yol 2 en güvenilir yoldur.

## Ekranda gördüğün bilgiler

- Host'ta kırmızı **"Bağlı"** şeridi: kim bağlı, **geçen süre** (`Süre: 1:05`, bir saati geçince `1:02:05`).
- Client'ta görüntüleyicinin alt çubuğunda **geçen süre** (ilk kare gelince sayaç başlar).
- Host'ta ayrıca "Gönderilen: ... MB" veri sayacı ve "Uyku engellendi" bilgisi.

## Güvenlik ve kararlılık notları

- **Hiçbir şey gizlenmiyor:** Host penceresi her zaman görünür, kim bağlandığını gösterir, "Bağlantıyı Kes"
  düğmesi vardır. Program arkada sessizce/görünmeden çalışmaz; bu bilerek böyle tasarlandı.
- **Pencere asla kilitlenip kalmaz:** kapat düğmesine basınca program en çok 5 saniye içinde kapanır (arka plan
  iş parçacıkları "daemon" olduğu için süreç onları beklemeden çıkabilir), `os._exit` gibi sert bir çıkış yoktur.
- **Basılı kalan tuş:** normal kapanışta/kopmada Host, basılı kalan her tuş ve fare düğmesini otomatik bırakır.
  Tek istisna: Host programı Görev Yöneticisi'nden zorla sonlandırılır veya bilgisayarın elektriği kesilirse, o
  anda basılı gönderilmiş bir tuş Windows'ta "basılı" görünebilir — ilgili tuşa bir kez fiziksel olarak
  dokununca kendiliğinden düzelir. Bu, bu tür araçların (TeamViewer dahil) ortak bir sınırıdır.
- **Uyku engeli kalıcı değil:** Host "Hazır" durumdayken bilgisayarın uykuya geçmesi engellenir; Host durunca bu
  kalkar. Program çökse bile Windows bu durumu otomatik temizler.
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
(Gerçek fare/klavye gönderen opsiyonel test `UZAK_REAL_INPUT=1` olmadan atlanır; çalıştırırsanız ekranı kaplayan bir
test penceresi açılır ve fare ~10 sn kendiliğinden hareket eder.)

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

`dist\KamilV1.exe --selftest` çalıştırılırsa ağa hiç çıkmadan (tünele bağlanmadan, gerçek girdi göndermeden)
içe aktarma, şifreleme, el sıkışma, ekran yakalama ve Tk'nin çalıştığını doğrulayıp bir sonuç dosyası yazar;
`0` çıkış kodu = hepsi geçti.

## İlk çalıştırmada karşılaşabilecekleriniz

- **SmartScreen "Bilinmeyen yayımcı" uyarısı:** exe imzasız olduğu için normaldir. "Daha fazla bilgi" →
  "Yine de çalıştır" ile devam edin.
- **Windows Güvenliği → Uygulama ve tarayıcı denetimi → Akıllı Uygulama Denetimi** açıksa imzasız exe'yi
  engelleyebilir; kapatmanız veya istisna eklemeniz gerekebilir.
- Antivirüs programları uzaktan-kontrol araçlarını (ekran yakalama + girdi enjeksiyonu + ağ) temkinli
  karşılayabilir; bu beklenen bir durumdur. Kendi derlediğiniz exe'nin özetini
  `certutil -hashfile dist\KamilV1.exe SHA256` ile alıp karşılaştırabilirsiniz.
- `--onedir` (klasörlü) derleme SmartScreen/AV ile sorun yaşarsanız alternatif olabilir: `build.bat` içindeki
  `--onefile` bayrağını `--onedir` ile değiştirip tekrar çalıştırın; sonuçta `dist\KamilV1\` klasörü oluşur.

## Gerçek ağda elle kontrol listesi

Otomatik testler gerçek tünel sunucularına bağlanmaz (tasarım gereği; sahte bore sunucusu kullanılır). Geliştirme
sırasında tek seferlik canlı bir deneme yapıldı: Host → gerçek bore.pub → Client, kare akışı ve girdi başarılı.
Kendi kurulumunuzda 5 dakikalık bir kontrol önerilir: (1) Başlat'a bas, ID'nin birkaç saniye içinde göründüğünü
doğrula; (2) ikinci bir bilgisayardan o ID+şifre ile bağlan; (3) Host'ta onay penceresinin çıktığını, kabul edince
görüntünün geldiğini doğrula; (4) fare hareketi/tıklama ve Türkçe karakterli yazı gönderip Host'ta uygulandığını
gözle kontrol et; (5) Client'ı kapatıp yeniden bağlan: bu kez onay penceresi çıkmamalı.

## Bilinen sınırlar

- Yalnızca birincil monitör paylaşılır, çoklu monitör seçimi yoktur.
- Pano (kopyala/yapıştır) senkronizasyonu, dosya transferi, ses yoktur.
- Oyun gibi ham tuş kodu okuyan uygulamalar, yazılan harfleri (Unicode metin olarak gönderildiği için)
  göremeyebilir; kısayollar (Ctrl+C vb.) yine de çalışır.
- Ayarlar `%LOCALAPPDATA%\Kamil V1\config.json` içindedir; `k` (parola türevi anahtarı), `uid` ve `trusted`
  listesi parola kadar hassastır — bu dosyaya ayrı bir şifreleme uygulanmaz (aynı Windows kullanıcısı olarak
  çalışan kötü amaçlı yazılım zaten okuyabilir).
