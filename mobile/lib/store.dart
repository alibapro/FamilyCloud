import 'dart:convert';
import 'dart:io';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path_provider/path_provider.dart';

/// Адрес сервера и токен — в защищённом хранилище (Keychain / Android Keystore).
class Creds {
  final String url;
  final String token;
  final String username;
  Creds(this.url, this.token, this.username);

  static const _s = FlutterSecureStorage(
    aOptions: AndroidOptions(encryptedSharedPreferences: true),
    iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock_this_device),
  );

  static Future<Creds?> load() async {
    final url = await _s.read(key: 'url');
    final token = await _s.read(key: 'token');
    final user = await _s.read(key: 'username');
    if (url == null || token == null) return null;
    return Creds(url, token, user ?? '');
  }

  Future<void> save() async {
    await _s.write(key: 'url', value: url);
    await _s.write(key: 'token', value: token);
    await _s.write(key: 'username', value: username);
  }

  static Future<void> clear() => _s.deleteAll();
}

/// Что уже выгружено: id файла в галерее -> хеш, дата изменения, размер.
class AssetRecord {
  final String sha;
  final int mod;
  final int size;
  AssetRecord(this.sha, this.mod, this.size);
  Map<String, dynamic> toJson() => {'s': sha, 'm': mod, 'z': size};
  factory AssetRecord.fromJson(Map<String, dynamic> j) =>
      AssetRecord(j['s'] as String, (j['m'] as num).toInt(), (j['z'] as num).toInt());
}

class LocalState {
  Map<String, AssetRecord> uploaded = {};
  int lastCleanupMs = 0;
  int cutoffDays = 30;
  bool biometric = false;

  static Future<File> _file() async {
    final dir = await getApplicationSupportDirectory();
    return File('${dir.path}/familycloud_state.json');
  }

  static Future<LocalState> load() async {
    final st = LocalState();
    try {
      final f = await _file();
      if (await f.exists()) {
        final j = jsonDecode(await f.readAsString()) as Map<String, dynamic>;
        st.lastCleanupMs = (j['lastCleanupMs'] as num?)?.toInt() ?? 0;
        st.cutoffDays = (j['cutoffDays'] as num?)?.toInt() ?? 30;
        st.biometric = j['biometric'] == true;
        final u = (j['uploaded'] as Map<String, dynamic>?) ?? {};
        st.uploaded = u.map((k, v) => MapEntry(k, AssetRecord.fromJson(v as Map<String, dynamic>)));
      }
    } catch (_) {
      // повреждённое состояние не опасно: файлы просто будут перепроверены (сервер скажет "exists")
    }
    return st;
  }

  /// Атомарная запись: во временный файл, потом переименование.
  Future<void> save() async {
    final f = await _file();
    final tmp = File('${f.path}.tmp');
    await tmp.writeAsString(jsonEncode({
      'lastCleanupMs': lastCleanupMs,
      'cutoffDays': cutoffDays,
      'biometric': biometric,
      'uploaded': uploaded.map((k, v) => MapEntry(k, v.toJson())),
    }), flush: true);
    await tmp.rename(f.path);
  }

  bool get cleanupDue => DateTime.now().millisecondsSinceEpoch - lastCleanupMs > 30 * 24 * 3600 * 1000;
}
