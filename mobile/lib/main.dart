import 'dart:io';

import 'package:flutter/material.dart';
import 'package:local_auth/local_auth.dart';
import 'package:photo_manager/photo_manager.dart';

import 'api.dart';
import 'background.dart';
import 'store.dart';
import 'sync.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initNotifications();
  runApp(const FamilyCloudApp());
}

class FamilyCloudApp extends StatelessWidget {
  const FamilyCloudApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'FamilyCloud',
        theme: ThemeData(colorSchemeSeed: Colors.indigo, useMaterial3: true),
        home: const StartScreen(),
      );
}

String fmtBytes(num b) {
  if (b >= 1024 * 1024 * 1024) return '${(b / 1024 / 1024 / 1024).toStringAsFixed(1)} ГБ';
  if (b >= 1024 * 1024) return '${(b / 1024 / 1024).toStringAsFixed(0)} МБ';
  return '${(b / 1024).toStringAsFixed(0)} КБ';
}

/// Проверяет вход и биометрию, затем открывает нужный экран.
class StartScreen extends StatefulWidget {
  const StartScreen({super.key});
  @override
  State<StartScreen> createState() => _StartScreenState();
}

class _StartScreenState extends State<StartScreen> {
  String? error;

  @override
  void initState() {
    super.initState();
    _go();
  }

  Future<void> _go() async {
    final creds = await Creds.load();
    final st = await LocalState.load();
    if (!mounted) return;
    if (creds == null) {
      Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const LoginScreen()));
      return;
    }
    if (st.biometric) {
      final ok = await LocalAuthentication().authenticate(localizedReason: 'Подтвердите вход в FamilyCloud');
      if (!ok) {
        setState(() => error = 'Вход не подтверждён');
        return;
      }
    }
    if (!mounted) return;
    Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => HomeScreen(creds: creds, st: st)));
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        body: Center(
          child: error == null
              ? const CircularProgressIndicator()
              : Column(mainAxisSize: MainAxisSize.min, children: [
                  Text(error!),
                  const SizedBox(height: 12),
                  FilledButton(onPressed: () { setState(() => error = null); _go(); }, child: const Text('Повторить')),
                ]),
        ),
      );
}

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final url = TextEditingController(text: 'https://');
  final user = TextEditingController();
  final pass = TextEditingController();
  bool busy = false;
  String? error;

  Future<void> _login() async {
    setState(() { busy = true; error = null; });
    try {
      final api = Api(url.text);
      final res = await api.login(user.text.trim(), pass.text, Platform.isIOS ? 'iPhone' : 'Android');
      final creds = Creds(api.baseUrl, res['token'] as String, res['username'] as String);
      await creds.save();
      pass.clear();
      final perm = await PhotoManager.requestPermissionExtend();
      if (!perm.hasAccess) throw ApiException(0, 'Нужен доступ к фото и видео');
      await requestNotificationPermission();
      final st = await LocalState.load();
      if (st.lastCleanupMs == 0) {
        st.lastCleanupMs = DateTime.now().millisecondsSinceEpoch;
        await st.save();
      }
      await registerBackground();
      if (!mounted) return;
      Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => HomeScreen(creds: creds, st: st)));
    } catch (e) {
      setState(() => error = e.toString());
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('FamilyCloud — вход')),
        body: ListView(padding: const EdgeInsets.all(20), children: [
          TextField(controller: url, decoration: const InputDecoration(labelText: 'Адрес сервера (https://…ts.net)'), keyboardType: TextInputType.url, autocorrect: false),
          TextField(controller: user, decoration: const InputDecoration(labelText: 'Логин'), autocorrect: false),
          TextField(controller: pass, decoration: const InputDecoration(labelText: 'Пароль'), obscureText: true),
          const SizedBox(height: 20),
          if (error != null) Text(error!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
          const SizedBox(height: 8),
          FilledButton(onPressed: busy ? null : _login, child: busy ? const SizedBox(height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2)) : const Text('Войти')),
        ]),
      );
}

class HomeScreen extends StatefulWidget {
  final Creds creds;
  final LocalState st;
  const HomeScreen({super.key, required this.creds, required this.st});
  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  late final Api api = Api(widget.creds.url, token: widget.creds.token);
  late final SyncEngine engine = SyncEngine(api, widget.st);
  Map<String, dynamic>? info;
  String status = '';
  bool busy = false;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _handle(Object e) async {
    if (e is ApiException && e.status == 401) {
      await Creds.clear();
      if (!mounted) return;
      Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const LoginScreen()));
      return;
    }
    setState(() => status = 'Ошибка: $e');
  }

  Future<void> _refresh() async {
    try {
      final m = await api.me();
      setState(() => info = m);
    } catch (e) {
      await _handle(e);
    }
  }

  Future<void> _sync() async {
    setState(() => busy = true);
    try {
      await engine.sync(onStatus: (s) => setState(() => status = s));
      await _refresh();
    } catch (e) {
      await _handle(e);
    } finally {
      setState(() => busy = false);
    }
  }

  Future<void> _cleanup() async {
    setState(() { busy = true; status = 'Сначала догружаю новые файлы…'; });
    try {
      await engine.sync(onStatus: (s) => setState(() => status = s));
      setState(() => status = 'Проверяю файлы на сервере…');
      final plan = await engine.planCleanup();
      if (!mounted) return;
      if (plan.assets.isEmpty) {
        setState(() => status = 'Нечего удалять: нет файлов старше ${widget.st.cutoffDays} дн., подтверждённых сервером.');
        return;
      }
      final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
          title: const Text('Освободить место?'),
          content: Text(
            'С телефона будут удалены ${plan.assets.length} файлов (${fmtBytes(plan.bytes)}) старше ${widget.st.cutoffDays} дней.\n\n'
            'Каждый из них сервер подтвердил на обоих дисках. '
            '${plan.skippedNotOnServer > 0 ? '${plan.skippedNotOnServer} файлов ещё не подтверждены — они останутся.\n\n' : ''}'
            '${Platform.isIOS ? 'После удаления очистите «Недавно удалённые» в Фото.' : ''}',
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Отмена')),
            FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Удалить с телефона')),
          ],
        ),
      );
      if (ok != true) {
        setState(() => status = 'Отменено');
        return;
      }
      final n = await engine.executeCleanup(plan);
      setState(() => status = 'Удалено с телефона: $n файлов. Они доступны на сервере.');
    } catch (e) {
      await _handle(e);
    } finally {
      setState(() => busy = false);
    }
  }

  Future<void> _logout() async {
    await api.logout();
    await cancelBackground();
    await Creds.clear();
    if (!mounted) return;
    Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const LoginScreen()));
  }

  @override
  Widget build(BuildContext context) {
    final st = widget.st;
    return Scaffold(
      appBar: AppBar(title: Text('FamilyCloud · ${widget.creds.username}'), actions: [
        IconButton(onPressed: busy ? null : _logout, icon: const Icon(Icons.logout), tooltip: 'Выйти'),
      ]),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: ListView(padding: const EdgeInsets.all(16), children: [
          if (st.cleanupDue)
            Card(
              color: Theme.of(context).colorScheme.tertiaryContainer,
              child: const ListTile(leading: Icon(Icons.cleaning_services), title: Text('Прошёл месяц — пора освободить место')),
            ),
          Card(
            child: ListTile(
              leading: const Icon(Icons.storage),
              title: Text(info == null ? 'Загрузка…' : 'На сервере: ${info!['files']} файлов, ${fmtBytes(info!['bytes'] as num)}'),
              subtitle: info == null
                  ? null
                  : Text('Свободно на дисках: ${fmtBytes(info!['free_bytes'] as num)}'
                      '${info!['mirror'] == true ? ' · копия на 2-м диске включена' : ' · ВНИМАНИЕ: второй диск не настроен'}'),
            ),
          ),
          const SizedBox(height: 12),
          FilledButton.icon(onPressed: busy ? null : _sync, icon: const Icon(Icons.cloud_upload), label: const Text('Загрузить сейчас')),
          const SizedBox(height: 8),
          FilledButton.tonalIcon(onPressed: busy ? null : _cleanup, icon: const Icon(Icons.delete_sweep), label: const Text('Освободить место')),
          const SizedBox(height: 12),
          if (busy) const LinearProgressIndicator(),
          Padding(padding: const EdgeInsets.symmetric(vertical: 8), child: Text(status)),
          const Divider(),
          ListTile(
            title: const Text('Удалять с телефона файлы старше'),
            trailing: DropdownButton<int>(
              value: st.cutoffDays,
              items: const [30, 60, 90, 180].map((d) => DropdownMenuItem(value: d, child: Text('$d дн.'))).toList(),
              onChanged: (v) async { st.cutoffDays = v ?? 30; await st.save(); setState(() {}); },
            ),
          ),
          SwitchListTile(
            title: const Text('Вход по отпечатку / Face ID'),
            value: st.biometric,
            onChanged: (v) async {
              if (v && !await LocalAuthentication().authenticate(localizedReason: 'Включить защиту входа')) return;
              st.biometric = v;
              await st.save();
              setState(() {});
            },
          ),
        ]),
      ),
    );
  }
}
