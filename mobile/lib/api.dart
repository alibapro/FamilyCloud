import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

class ApiException implements Exception {
  final int status;
  final String message;
  final Map<String, dynamic> body;
  ApiException(this.status, this.message, [this.body = const {}]);
  @override
  String toString() => message;
}

/// Клиент API сервера FamilyCloud.
class Api {
  final String baseUrl;
  final String? token;
  final http.Client _c = http.Client();

  Api(String base, {this.token})
      : baseUrl = base.trim().endsWith('/') ? base.trim().substring(0, base.trim().length - 1) : base.trim() {
    // Защита: только HTTPS (через Tailscale serve сертификат настоящий).
    if (!baseUrl.startsWith('https://')) {
      throw ApiException(0, 'Адрес сервера должен начинаться с https://');
    }
  }

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        if (token != null) 'Authorization': 'Bearer $token',
      };

  Uri _u(String path, [Map<String, String>? q]) => Uri.parse('$baseUrl$path').replace(queryParameters: q);

  Map<String, dynamic> _decode(http.Response r) {
    Map<String, dynamic> body = {};
    try {
      final d = jsonDecode(utf8.decode(r.bodyBytes));
      if (d is Map<String, dynamic>) body = d;
    } catch (_) {}
    return body;
  }

  Map<String, dynamic> _check(http.Response r) {
    final body = _decode(r);
    if (r.statusCode >= 200 && r.statusCode < 300) return body;
    throw ApiException(r.statusCode, body['detail']?.toString() ?? 'Ошибка сервера ${r.statusCode}', body);
  }

  Future<Map<String, dynamic>> login(String username, String password, String deviceName) async {
    final r = await _c
        .post(_u('/api/auth/login'),
            headers: _headers,
            body: jsonEncode({'username': username, 'password': password, 'device_name': deviceName}))
        .timeout(const Duration(seconds: 30));
    return _check(r);
  }

  Future<void> logout() async {
    try {
      await _c.post(_u('/api/auth/logout'), headers: _headers).timeout(const Duration(seconds: 15));
    } catch (_) {}
  }

  Future<Map<String, dynamic>> me() async =>
      _check(await _c.get(_u('/api/me'), headers: _headers).timeout(const Duration(seconds: 20)));

  Future<Map<String, dynamic>> initUpload(String filename, int size, String sha256, int takenAtMs) async {
    final r = await _c
        .post(_u('/api/uploads'),
            headers: _headers,
            body: jsonEncode({'filename': filename, 'size': size, 'sha256': sha256, 'taken_at': takenAtMs}))
        .timeout(const Duration(seconds: 30));
    return _check(r);
  }

  /// Отправляет кусок. Возвращает смещение, которое сервер реально имеет
  /// (при расхождении сервер отвечает 409 и мы продолжаем с его смещения).
  Future<int> putChunk(String uploadId, int offset, Uint8List data) async {
    final r = await _c
        .put(_u('/api/uploads/$uploadId', {'offset': '$offset'}),
            headers: {'Authorization': 'Bearer $token', 'Content-Type': 'application/octet-stream'}, body: data)
        .timeout(const Duration(minutes: 5));
    if (r.statusCode == 409) {
      return (_decode(r)['offset'] as num).toInt();
    }
    return (_check(r)['offset'] as num).toInt();
  }

  /// Завершение: сервер пересчитывает SHA-256, кладёт файл в папку и делает копию на 2-й диск.
  Future<Map<String, dynamic>> complete(String uploadId) async {
    final r = await _c
        .post(_u('/api/uploads/$uploadId/complete'), headers: _headers)
        .timeout(const Duration(minutes: 30));
    if (r.statusCode == 409) {
      return {'status': 'incomplete', 'offset': (_decode(r)['offset'] as num?)?.toInt() ?? 0};
    }
    return _check(r);
  }

  /// Возвращает хеши, которые надёжно лежат на сервере (на обоих дисках).
  Future<Set<String>> verify(List<String> hashes, {bool deep = false}) async {
    final r = await _c
        .post(_u('/api/files/verify'), headers: _headers, body: jsonEncode({'hashes': hashes, 'deep': deep}))
        .timeout(Duration(minutes: deep ? 60 : 5));
    final b = _check(r);
    return (b['safe'] as List).map((e) => e.toString()).toSet();
  }
}
