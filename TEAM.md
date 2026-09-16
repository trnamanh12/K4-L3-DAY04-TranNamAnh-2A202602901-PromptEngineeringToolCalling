# TEAM — Day04, K4-L3B

**Làm nhóm.** Mỗi người tự viết và commit phần INDIVIDUAL của mình.

## Thông tin bài nộp

- Tên nhóm: Nhóm Trần Nam Anh
- Người đại diện / MSSV: Trần Nam Anh — 2A202602901
- Tên repo: `K4-L3-DAY04-TranNamAnh-2A202602901-PromptEngineeringToolCalling`
- URL repo, nhánh nộp, commit chốt: `https://github.com/trnamanh12/K4-L3-DAY04-TranNamAnh-2A202602901-PromptEngineeringToolCalling`, nhánh `main`
- Deadline áp dụng và link thông báo đổi hạn nếu có: 12:00 ngày 16/9, anh labcoach thông báo hạn trên lớp ạ.

## Thành viên

| Họ và tên | MSSV | GitHub | Vai trò và công việc | File/commit/PR |
|---|---|---|---|---|
| Trần Nam Anh | 2A202602901 | `trnamanh12` | Nhóm trưởng: Tối ưu prompt, cấu hình tool schema, đánh giá v0-v3, viết 10 test cases nhóm, thực thi an toàn và báo cáo | `starter_v0/artifacts/`, `starter_v0/data/eval_group.json`, `starter_v0/transcripts/` |

## Nhận xét chung

- **Kết quả và bằng chứng**:
  * Đạt 100% độ chính xác (30/30 test cases) trên bộ đánh giá cơ bản `eval_base.json` (Run file: `starter_v0/runs/v3_B_base_gemini_20260916T093034694276.json`).
  * Đạt 100% độ chính xác (10/10 test cases) trên bộ đánh giá nhóm tự xây dựng `eval_group.json` (Run file: `starter_v0/runs/v3_B_group_gemini_20260916T093708164129.json`).
  * Thực hiện đánh giá an toàn 12 cases `eval_adversarial.json` (Run file: `starter_v0/runs/v3_B_adversarial_gemini_20260916T094127610431.json`), chứng minh ngăn chặn thành công việc trích xuất prompt nội bộ và bảo vệ dữ liệu nội bộ không bị gửi ra web search.
  * Phiên chat tương tác thực tế với transcript đầy đủ tại `starter_v0/transcripts/v3_gemini_20260916T094429236717.transcript.json`.
- **Thay đổi hiệu quả nhất**:
  * Phân tách rõ ràng enum `response_type` trong tool `clarify` ('text' cho trường hợp thiếu ID tự do, 'choice' cho danh sách tùy chọn môi trường).
  * Thiết lập Confirmation Boundary cho `create_ticket` trong cả `system_prompt.md` và `tools.yaml`, ngăn chặn tuyệt đối việc agent tự tiện tạo ticket khi chưa có xác nhận rõ ràng.
  * Bổ sung quy tắc gọi công cụ song song (`parallel tool calls`) cho so sánh thiết bị trong `system_prompt.md` giúp hoàn thành 100% bộ test base.
- **Giới hạn còn lại**:
  * Chưa có bộ lọc Regex Data Loss Prevention (DLP) tại tầng tiền xử lý để chủ động bóc tách mật khẩu nếu người dùng cố tình chèn chuỗi `password=...` vào nội dung ticket.
- **Cách phân công và tích hợp**:
  * Triển khai tuần tự theo 4 phiên bản kiểm thử thực nghiệm: v0 (Baseline starter) -> v1 (Cải thiện clarify & check_service_status) -> v2 (Confirmation Boundary) -> v3 (Parallel calls & Invalidation rules), đảm bảo mỗi phiên bản có một giả thuyết kỹ thuật riêng biệt và có bằng chứng đối soát trong `version_log.csv`.

## INDIVIDUAL

### Trần Nam Anh — 2A202602901

- **Phần việc và file/commit/PR**:
  * Phân tích 5 ca thất bại của bản gốc v0 trong `runs/v0_B_base_gemini_20260916T091128382288.json`.
  * Xây dựng và tinh chỉnh qua 3 phiên bản cải tiến v1, v2, v3 tại `starter_v0/artifacts/tools.yaml` và `starter_v0/artifacts/system_prompt.md`.
  * Thiết kế và chạy thử nghiệm 10 test case nhóm độc lập trong `starter_v0/data/eval_group.json` (5 single-turn, 5 multi-turn) đạt 10/10 PASS.
  * Thực thi kiểm thử an toàn đối kháng 12 case và ghi lại transcript trực tiếp trong `starter_v0/transcripts/`.
  * Hoàn thiện báo cáo kỹ thuật `starter_v0/artifacts/REPORT.md` và nhật ký `starter_v0/artifacts/version_log.csv`.
- **Quyết định, khó khăn và cách xử lý**:
  * *Khó khăn*: Ban đầu khi so sánh 2 máy tính (`H16`), LLM chỉ gọi tool `inspect_device` cho 1 máy rồi dừng lại.
  * *Cách xử lý*: Quyết định bổ sung chỉ dẫn rõ ràng về `Parallel Tool Calls and Comparisons` trong `system_prompt.md`, yêu cầu agent kích hoạt đồng thời nhiều tool call cho từng thực thể trong cùng một lượt.
  * *Quyết định*: Chia đều quá trình khắc phục lỗi qua các checkpoint v1, v2, v3 thay vì nạp tất cả vào v1, giúp theo dõi được tác động độc lập của từng giả thuyết kỹ thuật.
- **Điều đã học**:
  * Hiểu sự khác biệt giữa lỗi định tuyến công cụ (Tool Routing Error), lỗi tham số (Argument Error) và lỗi vượt ranh giới an toàn (Confirmation Boundary Violation).
  * Nắm vững kỹ thuật Prompt Engineering kết hợp Function Calling Schema: schema tốt giúp mô hình hiểu cấu trúc tham số, trong khi system prompt định hình hành vi và ranh giới nghiệp vụ phức tạp.
- **AI/công cụ đã dùng và cách kiểm tra**:
  * Sử dụng Antigravity để phân tích trace lỗi, viết test cases và soạn thảo tài liệu.
  * Kiểm tra độc lập bằng cách chạy script đánh giá `run_eval.py` trên môi trường Conda `vin`, đối chiếu kết quả từng trường hợp trong file JSON và transcript log thực tế.
- **Thời điểm đã tự nộp URL repo chung trên VLearn**:
  * Dự kiến nộp trước mốc kiểm tra tại lớp và hoàn thành bản chốt trước 12:00 ngày hôm sau của lab.
