mod backend;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(backend::BackendState::default())
        .invoke_handler(tauri::generate_handler![
            backend::launch_backend,
            backend::stop_backend,
            backend::backend_is_managed,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
