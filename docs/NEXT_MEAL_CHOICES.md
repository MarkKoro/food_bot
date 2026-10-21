# План следующего приёма пищи

Раздел «Что выбрать на следующий приём» по умолчанию свёрнут. Он даёт до трёх
качественных идей с компонентами по явно сохранённым canonical ограничениям.
Это отдельный план, не съеденная еда и не расчёт порции/КБЖУ. Фото и описание
фактически съеденного работают прежним способом. Нет AI, push, списка покупок
или расхода квоты анализа.

Food владеет своей native auth, доступом и read-only proxy `/api/next-meal`.
Core единолично хранит choice/receipts и canonical preferences. См.
[полный контракт и отдельный rollout](https://github.com/MarkKoro/progressor_core/blob/main/docs/NEXT_MEAL_CHOICES.md).
Изменение canonical ограничений требует повторного выбора; rev=0 направляет
в «Рацион и исключения», без угаданных предпочтений.

`FOOD_RESET_PROTOCOL = 1` в db.py защищает существующую очистку данных:
purge_user повышает только owner metadata `progressor_food_resets` в том же
savepoint с прежними удалениями. Paid account и реальные сроки сохраняются.
Adapter Core читает optional data_reset_revision из private snapshot. Proxy
передаёт текущую revision и проверяет её снова после ответа: устаревший Core
и старые операции не восстанавливают очищенный план. Global preferences
других продуктов остаются. Ошибка/окончание подписки не удаляют данные.

Пока отдельный `next-meal-v1-ready` не включён проверенным оператором после
завершённого update-all и подготовки совместимого fallback, Core-инструмент
закрыт. После включения откат к Food без reset protocol запрещается updater.
Обычная работа Food не зависит от доступности Core. Shared helper copies не
меняются и новое приложение Core внутри Food не создаётся.
