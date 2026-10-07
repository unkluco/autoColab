# AutoColab

Worker Python chạy trên Windows, tự điền dòng chứa marker trong notebook thuộc thư mục Google Drive đã đồng bộ. Marker được đặt trong `config.toml`; các ví dụ dưới đây dùng `@bot`. Dùng Python 3.11 trở lên và Codex CLI đã đăng nhập.

Mỗi lần khởi chạy bằng `start.bat`, `main.py`, `run.cmd` hoặc `start.ps1`, chương trình tự kiểm tra/tạo môi trường `.venv` ngay trong dự án, kiểm tra pip và cài các thư viện còn thiếu từ `requirements.txt`, chạy `pip check`, sau đó mới chạy host bằng Python trong `.venv`. Không cài thư viện vào Python toàn cục. Hiện worker chỉ dùng thư viện chuẩn nên `requirements.txt` chưa có gói bên ngoài. Log chuẩn bị môi trường nằm ở `work/setup.log`. Lần đầu có thể mất thêm thời gian tạo môi trường. Python, Codex CLI và ứng dụng đồng bộ Drive cần được chuẩn bị trên từng máy theo hướng dẫn sau.

## Chuẩn bị trên máy mới

Launcher có cửa sổ, chống chạy trùng và tự phục hồi của dự án được thiết kế cho Windows. Các bước dưới đây dùng PowerShell và bản Codex CLI chạy trực tiếp trên Windows.

| Thành phần | Chuẩn bị |
|---|---|
| Git | Cài [Git for Windows](https://git-scm.com/install/windows) để dùng `git clone`. |
| Python | Cài [Python cho Windows](https://www.python.org/downloads/windows/) phiên bản **3.11 trở lên**, có `venv` và pip. Lệnh `python` phải trỏ tới bản Python đó. |
| Node.js và npm | Cài [Node.js bản LTS](https://nodejs.org/en/download), kèm npm, để cài Codex CLI theo cách bên dưới. |
| Codex CLI | Cài CLI và đăng nhập tài khoản có quyền sử dụng Codex. Bản dự án này đã được kiểm tra với **Codex CLI 0.160.1**. |
| Google Drive | Cài [Google Drive for desktop](https://www.google.com/drive/download/), đăng nhập và bảo đảm thư mục notebook đọc/ghi được từ File Explorer. |
| Kết nối mạng | Cần khi cài đặt, đồng bộ Drive và gọi Codex. |

Sau khi cài công cụ, mở lại terminal để nhận PATH mới. Kiểm tra:

```powershell
git --version
python --version
node --version
npm.cmd --version
```

`python --version` phải trả về phiên bản ít nhất 3.11. Nếu `py` chạy được nhưng `python` chưa chạy được, cần chỉnh PATH/thiết lập Python để lệnh `python` hoạt động, vì `start.bat` tìm Python bằng lệnh này. Nếu dùng Conda, chạy `.\start.bat` từ terminal đã kích hoạt môi trường có Python phù hợp; nhấp đúp từ Explorer không nhận môi trường đã kích hoạt trong terminal đó.

### Cài và đăng nhập Codex CLI

Cài bản đã kiểm thử bằng npm. Dùng đuôi `.cmd` trong PowerShell để gọi wrapper Windows:

```powershell
npm.cmd install -g @openai/codex@0.160.1
codex.cmd --version
codex.cmd login
codex.cmd login status
```

Hoàn tất đăng nhập trong trình duyệt. Mỗi máy cần có phiên đăng nhập CLI riêng; AutoColab dùng phiên này cho các lời gọi `codex exec`. Cách cài bằng npm cũng được dùng trong [hướng dẫn Codex chính thức](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex); cách đăng nhập và kiểm tra phiên được mô tả trong [tài liệu xác thực OpenAI](https://learn.chatgpt.com/docs/auth).

### Clone và chỉnh cấu hình

Chọn vị trí lưu mã nguồn trên ổ cục bộ, ví dụ một thư mục dưới `C:\code`, rồi clone repo:

```powershell
git clone https://github.com/unkluco/autoColab.git autocolab
cd autocolab
```

Mở `config.toml` và sửa các giá trị có sẵn cho máy mới:

| Trường | Cần kiểm tra |
|---|---|
| `watch_folder` | Đường dẫn thư mục notebook Drive thực tế trên máy. Không giả định máy nào cũng có ổ `G:`. Chép đường dẫn từ File Explorer; dùng chuỗi TOML nháy đơn như `'G:\My Drive\Colab Notebooks'`. |
| `notebook.marker` | Chuỗi kích hoạt bạn sẽ viết trong notebook. Nếu đang đặt `#cout`, cần dùng `#cout`; muốn dùng các ví dụ `@bot` bên dưới thì đổi trường này thành `"@bot"`. |
| `notebook.include_outputs_in_context` | `true` gửi thêm output văn bản/lỗi; `false` chỉ gửi nội dung cell. Ảnh không được gửi ở cả hai chế độ. |
| `codex.command` | Thường giữ `"codex"`; lệnh phải có trong PATH, hoặc điền đường dẫn executable trên máy. |
| `codex.model` | Để `""` để dùng cấu hình model của CLI trên máy mới, hoặc đặt model bạn có quyền sử dụng. |
| `runtime.directory` | Giữ `"runtime"` để log, trạng thái và backup nằm trên ổ cục bộ cạnh mã nguồn. |

Giữ các file trong `prompts/`; đường dẫn tương đối được tính từ thư mục chứa file cấu hình. Drive cần đồng bộ thay đổi về file `.ipynb` trên máy thì host mới đọc được. AutoColab đọc thư mục đã đồng bộ, dùng phiên đăng nhập của Drive for desktop.

Kiểm tra trước khi mở server:

```powershell
python main.py --check
python main.py --dry-run
```

Lần chạy đầu tự tạo `.venv` và kiểm tra/cài thư viện. `--check` kiểm tra cấu hình, thư mục Drive, CLI và đăng nhập; `--dry-run` liệt kê vị trí marker. Hai lệnh này không gọi model hoặc sửa notebook. Sau khi kiểm tra thành công, nhấp đúp `start.bat`, hoặc chạy:

```powershell
.\start.bat
```

Giữ cửa sổ mở để xem log và để server hoạt động; đóng cửa sổ là dừng toàn bộ phiên. Khi sửa `config.toml`, đóng rồi mở lại server để áp dụng. Có thể chuẩn bị riêng môi trường trước mà chưa quét notebook bằng `python bootstrap.py --prepare-only`.

### Giữ cấu hình riêng trên từng máy

`config.toml` là cấu hình chung có trong repo. Nếu muốn tùy chỉnh đường dẫn/model mà không commit thay đổi vào file chung, tạo bản cục bộ:

```powershell
Copy-Item .\config.toml .\config.local.toml
```

Chỉnh `config.local.toml`, rồi chạy launcher với file đó:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run_host.ps1 -ConfigFile .\config.local.toml
```

`config.local.toml` đã được `.gitignore` loại khỏi Git. `start.bat` vẫn dùng `config.toml`; nó không tự chọn bản cục bộ. Có thể kiểm tra bản cục bộ bằng `python main.py --config .\config.local.toml --check`.

### Những dữ liệu không đi kèm khi clone

Repo gồm mã nguồn, launcher, `config.toml`, `requirements.txt`, `prompts/`, `tests/` và notebook mẫu trong `examples/`. `.gitignore` loại môi trường Python, `runtime/`, `work/`, log, backup trong runtime, cache, file tạm, cấu hình riêng và file xác thực. Máy mới tạo lại môi trường và dữ liệu chạy; đăng nhập Codex/Drive trên máy đó.

Notebook cá nhân nằm trong thư mục Drive mà `watch_folder` trỏ tới. `.gitignore` không loại mọi file `.ipynb`, vì notebook mẫu cần được lưu trong repo. Các bản log và backup đã xuất ra vị trí tùy chỉnh bên ngoài `runtime/` cần được quản lý riêng.

Trong repo đã có commit, `.gitignore` không tự bỏ theo dõi file đã được Git lưu. Nếu trước đây đã commit thư mục môi trường hoặc dữ liệu chạy, có thể bỏ chúng khỏi index, giữ nguyên file trên máy:

```powershell
git rm -r --cached --ignore-unmatch .venv runtime work outputs
git status --short
```

### Nếu máy mới chưa chạy được

| Thông báo/tình huống | Cách kiểm tra |
|---|---|
| Không tìm thấy `python` | Kiểm tra `python --version`, PATH và Python 3.11+; mở lại terminal sau khi cài. |
| Không tìm thấy Codex | Kiểm tra `codex.cmd --version`; bảo đảm thư mục npm global nằm trong PATH. |
| CLI chưa đăng nhập | Chạy `codex.cmd login`, rồi `codex.cmd login status`. |
| Thư mục quét không tồn tại | Mở Drive for desktop, kiểm tra đường dẫn `watch_folder`. Chế độ liên tục chờ thư mục trở lại; `--check` vẫn báo lỗi để bạn kiểm tra. |
| Không phát hiện marker | Kiểm tra `notebook.marker` khớp nội dung notebook và file đã đồng bộ về máy. |
| Chuẩn bị môi trường thất bại | Xem `work/setup.log`, kiểm tra mạng và quyền ghi thư mục dự án. |
| Báo server đã chạy | Dùng cửa sổ đang mở; cơ chế chống chạy trùng sẽ chặn phiên thứ hai trên máy. |

## Cấu trúc thư mục

```text
autocolab/
├── README.md             # Hướng dẫn cài đặt, sử dụng và vận hành
├── config.toml           # Cấu hình thư mục quét, marker, CLI và thời gian chờ
├── requirements.txt      # Các thư viện Python cần cho dự án
├── .gitignore            # Loại dữ liệu cục bộ và thông tin riêng khỏi Git
├── .gitattributes        # Quy ước xuống dòng và file nhị phân
├── start.bat             # Nhấp đúp để mở server và xem log
├── run.cmd               # Chuyển tới start.bat
├── run_host.ps1          # Giám sát host, chống chạy trùng và tự khởi động lại
├── start.ps1             # Chạy trực tiếp hoặc chạy nền
├── status.ps1            # Xem trạng thái host
├── stop.ps1              # Gửi yêu cầu dừng host
├── main.py               # Điểm vào chương trình và các tùy chọn dòng lệnh
├── bootstrap.py          # Kiểm tra/tạo .venv và chuẩn bị thư viện
├── config.py             # Đọc và kiểm tra cấu hình
├── controller.py         # Xử lý lệnh status/stop độc lập với môi trường host
├── worker.py             # Vòng quét, xử lý tuần tự, retry và cập nhật trạng thái
├── notebooks.py          # Đọc notebook, tìm marker, tạo ngữ cảnh, kiểm tra hash và lưu
├── solver.py             # Gọi Codex CLI, nhận phản hồi và kiểm soát giới hạn
├── single_instance.py    # Khóa chống nhiều host và xác minh phiên đang chạy
├── process_job.py        # Quản lý tiến trình CLI và tiến trình con trên Windows
├── storage_safety.py     # Kiểm tra phạm vi đường dẫn và phục hồi file tạm
├── prompts/
│   ├── main.txt          # Hướng dẫn chung cho mọi lời gọi
│   ├── code.txt          # Hướng dẫn riêng cho cell code
│   └── markdown.txt      # Hướng dẫn riêng cho cell Markdown
├── examples/
│   └── demo.ipynb        # Notebook mẫu để thử marker
├── tests/                # Kiểm thử notebook, CLI, lưu file và vòng đời host
├── .venv/                # Môi trường Python tự tạo trên máy
├── work/                 # Dữ liệu chuẩn bị môi trường, gồm setup.log
└── runtime/              # Log, trạng thái, backup và dữ liệu hoạt động
    ├── worker.log
    ├── codex-last.log
    ├── status.json
    ├── blocked-notebooks.json
    ├── rejected-answer.txt
    ├── backups/
    ├── artifacts/
    └── calls/            # Dữ liệu tạm của từng lời gọi CLI
```

`.venv/`, `work/` và `runtime/` là dữ liệu cục bộ, được tạo khi cần và không đi kèm khi clone. Các file trong `runtime/` xuất hiện tùy hoạt động của host; `rejected-answer.txt` chỉ được tạo khi phản hồi còn chứa marker. Cây trên dùng đường dẫn runtime mặc định; có thể đổi bằng `runtime.directory`.

Notebook cần xử lý nằm ở thư mục riêng do `watch_folder` chỉ định, hiện là `G:\My Drive\Colab Notebooks`. Thư mục này nằm ngoài dự án. Vai trò và cách sử dụng log/backup được mô tả thêm ở mục [Log, trạng thái và backup](#log-trạng-thái-và-backup).

## Khởi chạy

**Cách đơn giản nhất: nhấp đúp `start.bat`.** Cửa sổ CMD hiển thị log trực tiếp; giữ cửa sổ mở để host chạy. Đóng cửa sổ là dừng host và toàn bộ tiến trình con, kể cả Codex đang chạy. `run.cmd` cũng chuyển tới cùng cơ chế này.

`run_host.ps1` giữ Windows Job Object có `KILL_ON_JOB_CLOSE`; tiến trình khởi động Python được đưa vào job khi còn suspended rồi mới chạy, để các tiến trình con tự thuộc job. Nếu cửa sổ CMD hoặc supervisor bị tắt, job dừng cả cây tiến trình. Host lỗi thì launcher dừng toàn bộ cây cũ trước khi chạy lại, nghỉ tăng dần 5–120 giây. Sau một phiên chạy ít nhất 300 giây, thời gian nghỉ trở về mức đầu. Khi host không cập nhật tiến độ trong 900 giây, launcher cũng dừng cây tiến trình và chạy lại; cơ chế này bao phủ cả đọc Drive bị treo. Dừng chủ động, báo chạy trùng hoặc cấu hình không hợp lệ thì không chạy lại. Có thể nhấn Ctrl+C để yêu cầu dừng trong terminal; dùng nút đóng cửa sổ nếu muốn kết thúc toàn bộ phiên ngay. Nếu có một host đang chạy, cửa sổ mới báo lỗi chạy trùng và không mở host thứ hai.

Mở PowerShell tại thư mục dự án:

```powershell
python main.py --check
python main.py --dry-run
python main.py
```

- `--check`: kiểm tra cấu hình, thư mục, phiên bản CLI và đăng nhập; không gọi model.
- `--dry-run`: liệt kê notebook có marker; không gọi model và không sửa notebook.
- Không có tùy chọn: chạy liên tục. Nhấn Ctrl+C để dừng.
- `--once`: xử lý tối đa một marker trong toàn bộ thư mục rồi thoát.
- `start.bat` là launcher cho chế độ có cửa sổ và log; `run.cmd` là tên gọi tương đương.

Chạy nền với cửa sổ ẩn:

```powershell
.\start.ps1 -Background
.\status.ps1
.\stop.ps1
```

`start.ps1 -Background` là chế độ riêng, tiếp tục chạy sau khi launcher đóng; dùng `stop.ps1` để dừng. Nếu muốn đóng cửa sổ là dừng host, dùng `start.bat`.

Tự chạy lại và watchdog ở trên áp dụng cho `start.bat`/`run.cmd`. Chạy trực tiếp Python hoặc chế độ nền cũ chỉ dùng cơ chế phục hồi trong worker. Để chạy lâu dài với đầy đủ giám sát, dùng `start.bat`.

`stop.ps1` gửi yêu cầu tới worker đang hoạt động. Trong khoảng launcher đang chờ chạy lại và chưa có worker, đóng cửa sổ CMD để dừng phiên giám sát. Khóa riêng của supervisor vẫn được giữ trong khoảng nghỉ này, nên mở thêm `start.bat` sẽ bị chặn.

`--status` và `--stop` dùng controller riêng, không cài thư viện hoặc kiểm tra config/prompt trước khi điều khiển host. Cấu hình sai hoặc prompt bị xóa không chặn hai lệnh này. Status thiếu, hỏng hoặc sai kiểu dữ liệu được thay bằng trạng thái tối thiểu.

Lỗi Windows tạm thời lúc tạo tiến trình hoặc đọc thiết lập supervisor cũng được thử lại. Trước mỗi lần khởi động lại, supervisor đọc lại các thiết lập để theo dõi đúng runtime của worker mới. Worker xác nhận hash của file cấu hình đã được supervisor đọc; nếu config đổi trong lúc chuẩn bị môi trường, phiên dừng và yêu cầu mở lại thay vì chạy với hai bản thiết lập khác nhau. Khi không xác nhận được cây tiến trình cũ đã dừng, supervisor vẫn dừng hẳn để tránh chồng phiên.

Các script dùng `config.toml` bên cạnh chúng. Nếu PowerShell chặn script, vẫn có thể dùng `python main.py`, `python main.py --status` và `python main.py --stop`. Khi dừng, worker hủy lời gọi CLI đang chạy và giữ nguyên notebook nếu chưa ghi.

Trên Windows, named mutex `Global\AutoColabHost_v1` ngăn chạy nhiều host trên cùng máy, kể cả khi đổi config/runtime hoặc sao chép dự án sang thư mục khác. Khởi chạy trùng trả exit code `2`, không gọi Codex và không sửa notebook. Mutex thuộc hệ điều hành nên tự nhả khi tiến trình chủ bị tắt; không dựa vào PID file còn sót. Vẫn giữ khóa file theo runtime làm lớp bảo vệ bổ sung. Tham khảo cách dùng mutex và exit code `2` của [chatlms](https://github.com/unkluco/chatlms/blob/fa5fb681b02f09d0f16a06606b5d2137c99433b6/lms_blog_bot.py#L210).

Thông tin chủ host được lưu trong `%LOCALAPPDATA%\AutoColab\host.json` cùng PID, thời điểm tạo tiến trình, mã phiên và đường dẫn runtime thật. `--status` và `--stop` tìm đúng host đang chạy thay vì phụ thuộc runtime của config gọi lệnh. Nếu host thuộc tài khoản khác và không truy cập được thông tin chủ, chương trình vẫn chặn chạy trùng nhưng không gửi yêu cầu dừng tới một đường dẫn không xác minh được.

## Cách sử dụng notebook

Viết yêu cầu ở các dòng xung quanh; đặt marker tại dòng dành cho câu trả lời. Các ví dụ sau giả định `notebook.marker = "@bot"`. Có thể dùng `# @bot` trong cell code nếu muốn dòng đó vẫn là comment Python hợp lệ. Khi cấu hình marker khác, dùng đúng chuỗi đó thay cho `@bot`.

```python
# Viết hàm cộng hai số, đặt tên là add.
@bot chữ trên cùng dòng này cũng sẽ bị xóa
print(add(2, 3))
```

Trong cell Markdown, dùng cách tương tự:

```markdown
Nhận xét kết quả accuracy và loss ở trên.
@bot
```

## Luồng xử lý

1. Quét các file `.ipynb` theo thứ tự đường dẫn; có thể quét cả thư mục con. Bỏ qua `.ipynb_checkpoints`, symlink, junction Windows và thư mục runtime. Kiểm tra đường dẫn thực thuộc thư mục quét cả trước khi đọc và trước khi ghi.
2. Tìm marker đầu tiên trong `source` của các cell code/Markdown, theo thứ tự cell rồi dòng. Không tìm trong metadata, outputs hoặc cell raw.
3. Ghép prompt chính và prompt của loại cell thành hướng dẫn riêng cho lời gọi CLI. Chuyển toàn bộ nội dung cell thành văn bản, giữ thứ tự. Có thêm output văn bản/lỗi khi bật `include_outputs_in_context`; metadata và ảnh không được gửi. Cell raw được đưa vào ngữ cảnh nhưng không sửa.
4. Truyền ngữ cảnh qua UTF-8 stdin vào `codex exec`; đợi lời gọi hoàn tất. CLI chạy read-only, phiên tạm; mặc định tắt MCP/công cụ không cần cho sinh nội dung trong riêng lời gọi này. Vẫn dùng đăng nhập và model trong cấu hình Codex của bạn.
5. Đọc file chứa câu trả lời cuối, không lấy progress/log từ console. Không bỏ Markdown fences, không sửa code, không tự kiểm tra cú pháp; chèn nguyên văn phản hồi hợp lệ. Phản hồi rỗng, sai encoding hoặc quá lớn thì giữ nguyên marker, nghỉ riêng file đó và cho file khác tiếp tục. Nếu phản hồi chứa lại marker, giữ nguyên notebook và tạm chặn đúng bản nội dung đó, kể cả sau restart, để tránh gọi vô hạn. Chỉnh notebook để thử lại; phản hồi gần nhất bị chặn nằm ở `runtime/rejected-answer.txt`. CLI lỗi dịch vụ/runtime thì nghỉ chung với thời gian tăng dần trước khi gọi lại. Nếu không dọn được tiến trình CLI, host thoát lỗi để launcher dừng cả cây rồi phục hồi.
6. Xóa toàn bộ dòng chứa marker đầu tiên và thay bằng phản hồi. Host chỉ thêm dấu xuống dòng nếu cần để tách với dòng tiếp theo; độ thụt lề do Codex trả về được giữ nguyên. Các cell khác, metadata, ID, outputs và execution count giữ nguyên. Định dạng thụt lề của JSON file có thể thay đổi sau khi lưu.
7. Ghi file tạm cùng thư mục. Tính lại SHA-256 trước khi tạo backup và ngay trước khi thay thế. Nếu khác bản đã đọc, bỏ câu trả lời và quét lại ngay; sau 3 xung đột liên tiếp, tạm bỏ qua file đó 30 giây để các file khác tiếp tục. Nếu giống, thay file bằng bản mới; mặc định có backup bản gốc bên ngoài Drive. Backup mới không sử dụng được xóa nếu xảy ra xung đột hoặc lỗi ghi.
8. Sau mỗi vòng bình thường, chọn ngẫu nhiên khoảng nghỉ 4–7 giây rồi quét lại. Mỗi vòng chỉ ghi tối đa một marker; marker tiếp theo được giải với ngữ cảnh notebook đã cập nhật. Trong lúc CLI chạy, không quét hay gọi CLI song song.

Mỗi lời gọi CLI trên Windows có Job Object riêng. Một launcher nhỏ đợi host gán vào job rồi mới được phép khởi động CLI; host dừng các tiến trình con còn sót cả khi đã nhận câu trả lời thành công. Job của cả phiên server vẫn giữ vai trò bảo vệ bên ngoài.

Backup được ghi hoàn chỉnh trước khi công bố tên chính thức. Khi các backup hiện có đang trong giới hạn, chúng chỉ được dọn sau khi notebook đã ghi thành công; hash đổi hoặc lỗi ghi giữ nguyên các backup cũ. Trong lúc chuyển giao có thể cần thêm dung lượng bằng một bản gốc notebook. Nếu ổ không đủ chỗ, giữ nguyên notebook. Lỗi dọn sau khi lưu được cảnh báo; lần sau sửa phần vượt giới hạn trước khi tạo thêm, để không tích lũy không giới hạn.

Hash kiểm tra bản file trên máy, không bảo đảm Google Drive đã nhận mọi chỉnh sửa từ đám mây. Khoảng kiểm tra rồi thay file vẫn có một cửa sổ rất nhỏ nếu ứng dụng khác ghi cùng lúc. Thời gian nhận thay đổi từ Drive phụ thuộc đồng bộ. Khoảng nghỉ ngẫu nhiên không bảo đảm tránh quota hoặc giới hạn của dịch vụ.

## Cấu hình

Chỉnh `config.toml`. Các đường dẫn tương đối tính từ thư mục chứa file cấu hình.

- `watch_folder`: hiện là `G:\My Drive\Colab Notebooks`.
- `recursive`: quét thư mục con.
- `scan.min_seconds` / `scan.max_seconds`: khoảng nghỉ ngẫu nhiên sau vòng xử lý.
- `notebook.marker`, `cell_types`: chuỗi kích hoạt và loại cell được xử lý.
- `notebook.include_outputs_in_context`: nếu bật, thêm output văn bản, text/plain và lỗi; không thêm ảnh/base64.
- `notebook.max_bytes`: giới hạn notebook được đọc. Giá trị mặc định trong chương trình là 20 MiB; cấu hình kèm dự án có thể đặt khác. File lớn hơn được giữ nguyên và báo lỗi; không cắt bớt ngữ cảnh để giải.
- `codex.command`: lệnh `codex` hoặc đường dẫn executable; trên Windows tự tìm native executable từ npm để tránh lỗi quoting của shell.
- `codex.model`: để trống dùng cấu hình CLI hiện tại; có thể chọn model riêng.
- `codex.timeout_seconds`: giới hạn thời gian cho một lần gọi.
- `codex.context_max_chars`: giới hạn ngữ cảnh và câu trả lời, mặc định 2.000.000 ký tự; không chèn câu trả lời bị cắt dở.
- `codex.log_max_bytes`: giới hạn cứng log một lời gọi CLI, mặc định 10 MiB. CLI vượt giới hạn bị dừng.
- `codex.*_prompt_file`: ba file prompt.
- `codex.disable_mcp_servers`, `disabled_features`, `web_search`: tùy chọn riêng cho các lời gọi CLI; không sửa cấu hình Codex toàn cục.
- `retry.seconds`: khoảng nghỉ trước khi thử lại file lỗi có cùng mtime/size, lấy từ cấu hình. File khác vẫn có thể được xử lý. File thay đổi được thử lại ở vòng sau.
- `retry.global_initial_seconds`, `global_max_seconds`: nghỉ chung khi CLI lỗi, tăng dần từ mức ban đầu đến mức tối đa trong cấu hình, có dao động ngẫu nhiên. Một lần CLI thành công đặt lại thời gian nghỉ. File vừa lỗi còn được nghỉ riêng để lần hồi phục có thể thử file khác.
- `retry.conflict_limit`, `conflict_seconds`: sau bao nhiêu xung đột liên tiếp thì tạm bỏ qua file, mặc định 3 lần và 30 giây.
- `runtime.directory`, `backup_enabled`, `log_level`, `log_max_bytes`, `log_backup_count`: nơi lưu và mức log/backup.
- `runtime.call_max_bytes`: giới hạn tổng dung lượng `runtime/calls/`, mặc định 64 MiB. Host dọn thư mục lần gọi có dấu sở hữu của AutoColab trước lời gọi tiếp theo; không xóa thư mục không nhận diện được. CLI vượt giới hạn bị dừng; dữ liệu sót không thể dọn và vượt giới hạn thì cần kiểm tra trước khi mở lại.
- `runtime.status_retry_delays_ms`: khoảng nghỉ giữa các lần thử thay file trạng thái khi bị khóa/từ chối truy cập. Mặc định `[50, 100, 200]` mili giây: một lần đầu và tối đa ba lần thử lại. Danh sách rỗng tắt thử lại; tổng thời gian chờ được giới hạn để worker không bị giữ quá lâu.
- `runtime.backup_max_count`, `backup_max_bytes`, `backup_max_age_days`: tối đa 100 bản, 512 MiB, tuổi 14 ngày. Dọn khi chuẩn bị tạo backup tiếp theo; chỉ xóa các file backup do AutoColab nhận diện được. Nếu một backup bắt buộc vượt dung lượng cho phép, notebook được giữ nguyên.
- `supervisor.restart_initial_seconds`, `restart_max_seconds`, `restart_reset_seconds`: khoảng nghỉ chạy lại host và thời gian đặt lại, mặc định 5, 120, 300 giây.
- `supervisor.watchdog_seconds`: giới hạn không có tiến độ, mặc định 900 giây; phải ít nhất bằng `codex.timeout_seconds + 30`. Host cập nhật trạng thái định kỳ khi nghỉ, để khoảng nghỉ hợp lệ không bị coi là treo.

`replace_entire_marker_line`, `overwrite`, `check_hash_before_write` phải là `true`, theo quy ước của bản này. Cấu hình được đọc lúc khởi động: dừng và chạy lại sau khi đổi. Nội dung prompt được đọc mỗi lần gọi nên có thể chỉnh trực tiếp khi worker đang chạy.

Trên Windows, hướng dẫn Codex đi qua dòng lệnh nên prompt/config quá dài được phát hiện và báo lỗi cấu hình trước khi gọi. `--check` kiểm tra điều này cho các loại cell được bật; sửa prompt quá dài khi đang chạy cũng dừng phiên thay vì retry mãi.

Không tự thực thi notebook. Code/Markdown do Codex tạo có thể cần bạn sửa tay. Các ô đã sửa vẫn giữ output cũ cho đến khi bạn chạy lại notebook.

## Log, trạng thái và backup

- `runtime/worker.log`: file nào đang xử lý, đã lưu, xung đột hoặc lỗi; tự xoay log theo cấu hình.
- `runtime/codex-last.log`: chẩn đoán lần gọi CLI gần nhất; có thể chứa nội dung notebook.
- `runtime/status.json`: trạng thái, PID, vị trí cell và số lần đã lưu/lỗi/xung đột trong phiên.
- `runtime/backups/`: byte gốc của notebook trước mỗi lần ghi thành công; tên có thời gian và mã đường dẫn để phân biệt các file trùng tên. Có thể chép bản cần phục hồi về đường dẫn notebook gốc; bản cũ được dọn theo giới hạn cấu hình.
- `runtime/blocked-notebooks.json`: hash các bản notebook bị chặn vì phản hồi còn marker. Tự bỏ chặn khi nội dung file thay đổi. File này hỏng sẽ báo lỗi cấu hình thay vì tự mất bảo vệ khi restart.
- `runtime/artifacts/`: nhật ký đường dẫn file tạm do host mới tạo. Khi Drive trở lại, host dọn file còn sót được ghi nhận và còn nằm trong phạm vi cho phép. Không dọn chung mọi file `.tmp`; file sót từ bản cũ chưa có nhật ký hoặc thư mục CLI chưa có dấu sở hữu cần kiểm tra riêng.

Khi Windows từ chối thay file trạng thái vì quyền truy cập hoặc khóa file, worker thử lại theo `status_retry_delays_ms`, dùng cùng file tạm hoàn chỉnh. Nếu vẫn lỗi, cảnh báo có giới hạn tần suất và tiếp tục công việc; lần cập nhật sau vẫn thử lại. Lỗi khác như đầy ổ đĩa hoặc dữ liệu không thể chuyển thành JSON không dùng các lần thử lại này. Chế độ liên tục chờ khi Drive chưa sẵn sàng hoặc tạm mất kết nối. `--check`, `--dry-run`, `--once` vẫn báo lỗi ngay nếu thư mục không tồn tại. Khi không thể cập nhật trạng thái đủ lâu, watchdog của launcher chạy lại cả phiên. Đây là cơ chế phục hồi, không phải bằng chứng chương trình đã được thử liên tục 24 giờ.

Thư mục runtime nằm trên ổ cục bộ, ngoài Drive theo mặc định. Giữ runtime trên ổ cục bộ để supervisor đọc tiến độ độc lập với ổ đồng bộ. Trên Windows chỉ cho phép một host AutoColab trên máy; thông tin trạng thái và backup nằm ở runtime của host đó.

## Kiểm thử

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests sử dụng thư mục tạm và solver giả, không chạm Drive hoặc gọi model. Muốn thử CLI trên notebook mẫu, đặt `notebook.marker = "@bot"` trong cấu hình để khớp marker của mẫu, rồi chạy:

```powershell
python main.py --watch-folder .\examples --once
```

Lệnh này ghi đè notebook mẫu; vẫn tạo backup theo cấu hình. Có thể chạy lại để xử lý marker tiếp theo.
