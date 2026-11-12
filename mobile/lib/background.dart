import 'dart:ui';

import 'package:flutter/widgets.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:photo_manager/photo_manager.dart';
import 'package:workmanager/workmanager.dart';

import 'api.dart';
import 'store.dart';
import 'sync.dart';

const syncTask = 'familycloud.sync';

final _notifications = FlutterLocalNotificationsPlugin();

Future<void> initNotifications() async {
  await _notifications.initialize(const InitializationSettings(
    android: AndroidInitializationSettings('@mipmap/ic_launcher'),
    iOS: DarwinInitializationSettings(),
  ));
}

/// Android 13+: разрешение на уведомления нужно запросить явно.
Future<void> requestNotificationPermission() async {
  await _notifications
      .resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>()
      ?.requestNotificationsPermission();
}

Future<void> showCleanupReminder() async {
  await initNotifications();
  await _notifications.show(
    1,
    'Пора освободить место',
    'Фото и видео старше месяца уже на домашнем сервере. Откройте FamilyCloud → «Освободить место».',
    const NotificationDetails(
      android: AndroidNotificationDetails('cleanup', 'Ежемесячная очистка'),
      iOS: DarwinNotificationDetails(),
    ),
  );
}

/// Фоновая задача: загрузка новых файлов + напоминание раз в месяц.
@pragma('vm:entry-point')
void callbackDispatcher() {
  Workmanager().executeTask((task, input) async {
    WidgetsFlutterBinding.ensureInitialized();
    DartPluginRegistrant.ensureInitialized();
    try {
      final creds = await Creds.load();
      if (creds == null) return true;
      final perm = await PhotoManager.requestPermissionExtend();
      if (!perm.hasAccess) return true;
      final st = await LocalState.load();
      await SyncEngine(Api(creds.url, token: creds.token), st).sync();
      if (st.cleanupDue) await showCleanupReminder();
      return true;
    } catch (_) {
      return false; // система повторит позже
    }
  });
}

Future<void> registerBackground() async {
  await Workmanager().initialize(callbackDispatcher);
  await Workmanager().registerPeriodicTask(
    syncTask,
    syncTask,
    frequency: const Duration(hours: 6),
    constraints: Constraints(
      networkType: NetworkType.unmetered, // только Wi-Fi
      requiresBatteryNotLow: true,
    ),
  );
}

Future<void> cancelBackground() => Workmanager().cancelAll();
