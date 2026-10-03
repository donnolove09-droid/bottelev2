# Telegram Bot Manager 5.0 (Gaming UI)

## Tính năng mới
- **Giao diện Gaming 5.0**: neon, HUD, tab điều hướng, toast, responsive cho điện thoại.
- **Lấy ID phòng chat tự động**: thêm bot vào nhóm/kênh, hoặc có người nhắn → ID hiện ở tab *PHÒNG CHAT* (bấm để copy). Trong nhóm gõ `/id` để bot trả về Chat ID + ID của bạn (reply ai thì ra ID người đó). Có thêm ô nhập thủ công: ID, `@username` hoặc link `t.me/...`.
- **Gửi file**: upload ảnh/video/âm thanh/tài liệu (≤ 50MB) rồi gửi vào phòng chat từ dashboard; có kho file và lệnh `/file` (danh sách nút bấm) / `/file <id>` để thành viên tự nhận file.

## Slash commands
/help, /id, /star, /file, /aov, /mod, /ff, /ban, /unl

> Lưu ý: mọi thành viên trong phòng có bot đều dùng được `/file` để lấy file trong kho. Đừng upload file nhạy cảm.

## Deploy Render
1. Upload code lên GitHub.
2. Render → New → Blueprint hoặc Web Service.
3. Build: `pip install -r requirements.txt`
4. Start: `gunicorn bot:app`
5. Environment variable: `ADMIN_KEY=<chuỗi-bí-mật-dài>`

SQLite và thư mục `uploads/` nằm trên Persistent Disk (`render.yaml` đã khai báo 1GB) nên không mất khi redeploy.
