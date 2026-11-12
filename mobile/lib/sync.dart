import 'dart:async';
import 'dart:io';
import 'dart:math';

import 'package:convert/convert.dart';
import 'package:crypto/crypto.dart';
import 'package:photo_manager/photo_manager.dart';

import 'api.dart';
import 'store.dart';

class CleanupPlan {
  final List<AssetEntity> assets;
  final int bytes;
  final int skippedNotOnServer;
  CleanupPlan(this.assets, this.bytes, this.skippedNotOnServer);
}

class SyncEngine {
  static const int chunkSize = 8 * 1024 * 1024;
  static bool _running = false;

  final Api api;
  final LocalState st;
  SyncEngine(this.api, this.st);

  static Future<String> sha256OfFile(File f) async {
    final out = AccumulatorSink<Digest>();
    final input = sha256.startChunkedConversion(out);
    await for (final chunk in f.openRead()) {
      input.add(chunk);
    }
    input.close();
    return out.events.single.toString();
  }

  static int _mod(AssetEntity a) => a.modifiedDateTime.millisecondsSinceEpoch;

  Future<List<AssetEntity>> _allAssets() async {
    final paths = await PhotoManager.getAssetPathList(type: RequestType.common, onlyAll: true);
    if (paths.isEmpty) return [];
    final all = paths.first;
    final n = await all.assetCountAsync;
    final out = <AssetEntity>[];
    for (var i = 0; i < n; i += 500) {
      out.addAll(await all.getAssetListRange(start: i, end: min(i + 500, n)));
    }
    return out;
  }

  Future<T> _retry<T>(Future<T> Function() fn, {int attempts = 5}) async {
    for (var i = 1;; i++) {
      try {
        return await fn();
      } on ApiException catch (e) {
        // 4xx (кроме 408/429) — повтор не поможет
        if (e.status >= 400 && e.status < 500 && e.status != 408 && e.status != 429) rethrow;
        if (i >= attempts) rethrow;
      } catch (_) {
        if (i >= attempts) rethrow;
      }
      await Future.delayed(Duration(seconds: 2 * i * i));
    }
  }

  /// Загружает все новые/изменённые фото и видео. Возвращает число загруженных.
  Future<int> sync({void Function(String status)? onStatus}) async {
    if (_running) return 0;
    _running = true;
    var count = 0;
    try {
      final assets = await _allAssets();
      final todo = assets.where((a) {
        final r = st.uploaded[a.id];
        return r == null || r.mod != _mod(a);
      }).toList();
      for (var i = 0; i < todo.length; i++) {
        final a = todo[i];
        final file = await a.originFile;
        if (file == null) continue;
        final size = await file.length();
        final title = await a.titleAsync;
        final name = title.isEmpty ? '${a.id.replaceAll('/', '_')}.bin' : title;
        onStatus?.call('Загрузка ${i + 1} из ${todo.length}: $name');
        final sha = await sha256OfFile(file);
        await _uploadFile(file, name, size, sha, a.createDateTime.millisecondsSinceEpoch);
        st.uploaded[a.id] = AssetRecord(sha, _mod(a), size);
        count++;
        if (count % 10 == 0) await st.save();
      }
      onStatus?.call('Готово. Загружено новых файлов: $count');
    } finally {
      await st.save();
      _running = false;
    }
    return count;
  }

  Future<void> _uploadFile(File f, String name, int size, String sha, int takenAt) async {
    final init = await _retry(() => api.initUpload(name, size, sha, takenAt));
    if (init['status'] == 'exists') return;
    final id = init['upload_id'] as String;
    var offset = (init['offset'] as num).toInt();

    for (var round = 0; round < 3; round++) {
      final raf = await f.open();
      try {
        while (offset < size) {
          await raf.setPosition(offset);
          final data = await raf.read(min(chunkSize, size - offset));
          final from = offset;
          offset = await _retry(() => api.putChunk(id, from, data));
        }
      } finally {
        await raf.close();
      }
      final res = await _retry(() => api.complete(id));
      final status = res['status'];
      if (status == 'stored' || status == 'exists') return;
      if (status == 'incomplete') {
        offset = (res['offset'] as num).toInt();
        continue;
      }
      throw ApiException(0, 'Неожиданный ответ сервера: $res');
    }
    throw ApiException(0, 'Не удалось завершить загрузку $name');
  }

  /// Собирает список файлов старше N дней, которые сервер подтвердил на ОБОИХ дисках.
  Future<CleanupPlan> planCleanup() async {
    final cutoff = DateTime.now().subtract(Duration(days: st.cutoffDays));
    final assets = await _allAssets();
    final cands = assets.where((a) {
      final r = st.uploaded[a.id];
      // файл не менялся после загрузки (иначе на сервере старая версия)
      return r != null && r.mod == _mod(a) && a.createDateTime.isBefore(cutoff);
    }).toList();

    final safe = <String>{};
    final hashes = cands.map((a) => st.uploaded[a.id]!.sha).toSet().toList();
    for (var i = 0; i < hashes.length; i += 1000) {
      safe.addAll(await api.verify(hashes.sublist(i, min(i + 1000, hashes.length))));
    }
    final ok = cands.where((a) => safe.contains(st.uploaded[a.id]!.sha)).toList();
    final bytes = ok.fold<int>(0, (s, a) => s + st.uploaded[a.id]!.size);
    return CleanupPlan(ok, bytes, cands.length - ok.length);
  }

  /// Удаляет с телефона. Система покажет своё окно подтверждения.
  Future<int> executeCleanup(CleanupPlan plan) async {
    var deleted = 0;
    // Повторная проверка непосредственно перед удалением
    final hashes = plan.assets.map((a) => st.uploaded[a.id]!.sha).toSet().toList();
    final safe = <String>{};
    for (var i = 0; i < hashes.length; i += 1000) {
      safe.addAll(await api.verify(hashes.sublist(i, min(i + 1000, hashes.length))));
    }
    final ids = plan.assets.where((a) => safe.contains(st.uploaded[a.id]!.sha)).map((a) => a.id).toList();
    for (var i = 0; i < ids.length; i += 500) {
      final batch = ids.sublist(i, min(i + 500, ids.length));
      final done = await PhotoManager.editor.deleteWithIds(batch);
      for (final id in done) {
        st.uploaded.remove(id);
      }
      deleted += done.length;
      if (done.isEmpty) break; // пользователь отменил в системном окне
    }
    st.lastCleanupMs = DateTime.now().millisecondsSinceEpoch;
    await st.save();
    return deleted;
  }
}
